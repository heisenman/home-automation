// Minimal SQLite VFS over the ESP-IDF FATFS POSIX layer (SD card at /sdcard).
//
// Scope (ADR-0022 / ha_replica spike): the rung DB file is NEVER accessed
// concurrently — the server writes whole-file replicas, the panel only reads,
// and the spike writes once, exclusively. So all lock methods are no-ops. This
// deliberately sidesteps the unix-VFS fcntl(F_SETLK) gap on FATFS (FATFS does
// not implement advisory locking) which is the usual reason stock SQLite fails
// on an SD card. Do NOT reuse this VFS for any multi-writer scenario.
//
// Built with SQLITE_OS_OTHER=1, so we own sqlite3_os_init/_os_end and register
// this as the default VFS.

#include "sqlite3.h"
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include "esp_random.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define ESPVFS_MAXPATH 512

// Spike tracing: set to 1 to log every VFS call (captured via the debug firehose).
// MUST be 0 for the full-scale run — per-write logging over MQTT floods and skews timing.
#define ESPVFS_TRACE 0
#if ESPVFS_TRACE
#define VT(...) ESP_LOGW("espvfs", __VA_ARGS__)
#else
#define VT(...)
#endif

typedef struct EspFile {
    sqlite3_file base;   // must be first
    int fd;
} EspFile;

static int espClose(sqlite3_file *pFile) {
    EspFile *p = (EspFile *)pFile;
    if (p->fd >= 0) { close(p->fd); p->fd = -1; }
    return SQLITE_OK;
}

static int espRead(sqlite3_file *pFile, void *buf, int amt, sqlite3_int64 ofst) {
    EspFile *p = (EspFile *)pFile;
    // CRITICAL: FatFs f_lseek() beyond EOF on a writable file EXTENDS the file with zeros
    // (unlike POSIX, where only a write extends). So we must NEVER seek past the current
    // end for a read — that would silently grow the DB and corrupt its header. Determine
    // the size first and short-read (zero-fill) anything at/after EOF without seeking there.
    struct stat st;
    off_t size;
    if (fstat(p->fd, &st) == 0) size = st.st_size;
    else { size = lseek(p->fd, 0, SEEK_END); if (size < 0) { VT("read size-probe fail fd=%d", p->fd); return SQLITE_IOERR_READ; } }

    if (ofst >= size) {                    // wholly past EOF: zero-fill, do not seek/extend
        memset(buf, 0, amt);
        VT("read fd=%d ofst=%lld amt=%d PAST-EOF(size=%lld) short", p->fd, (long long)ofst, amt, (long long)size);
        return SQLITE_IOERR_SHORT_READ;
    }
    if (lseek(p->fd, (off_t)ofst, SEEK_SET) != (off_t)ofst) { VT("read seek fail fd=%d ofst=%lld", p->fd, (long long)ofst); return SQLITE_IOERR_READ; }
    int want = amt;
    if ((sqlite3_int64)ofst + amt > (sqlite3_int64)size) want = (int)(size - ofst);   // clamp to real data
    int got = 0;
    while (got < want) {
        int n = read(p->fd, (char *)buf + got, want - got);
        if (n < 0) { VT("read err fd=%d", p->fd); return SQLITE_IOERR_READ; }
        if (n == 0) break;
        got += n;
    }
    VT("read fd=%d ofst=%lld amt=%d got=%d (size=%lld)", p->fd, (long long)ofst, amt, got, (long long)size);
    if (got < amt) {                       // short read: zero-fill the tail
        memset((char *)buf + got, 0, amt - got);
        return SQLITE_IOERR_SHORT_READ;
    }
    return SQLITE_OK;
}

static int espWrite(sqlite3_file *pFile, const void *buf, int amt, sqlite3_int64 ofst) {
    EspFile *p = (EspFile *)pFile;
    off_t got_off = lseek(p->fd, (off_t)ofst, SEEK_SET);
    if (got_off != (off_t)ofst) { VT("write seek fail fd=%d ofst=%lld got=%ld errno=%d", p->fd, (long long)ofst, (long)got_off, errno); return SQLITE_IOERR_WRITE; }
    int put = 0;
    while (put < amt) {
        int n = write(p->fd, (const char *)buf + put, amt - put);
        if (n < 0) { VT("write err fd=%d put=%d errno=%d", p->fd, put, errno); return SQLITE_IOERR_WRITE; }
        if (n == 0) { VT("write returned 0 fd=%d put=%d/%d errno=%d", p->fd, put, amt, errno); return SQLITE_IOERR_WRITE; }
        put += n;
    }
    VT("write fd=%d ofst=%lld amt=%d", p->fd, (long long)ofst, amt);
    return SQLITE_OK;
}

static int espTruncate(sqlite3_file *pFile, sqlite3_int64 size) {
    EspFile *p = (EspFile *)pFile;
    int rc = ftruncate(p->fd, (off_t)size);
    VT("truncate fd=%d size=%lld rc=%d errno=%d", p->fd, (long long)size, rc, errno);
    if (rc != 0) return SQLITE_IOERR_TRUNCATE;
    return SQLITE_OK;
}

static int espSync(sqlite3_file *pFile, int flags) {
    EspFile *p = (EspFile *)pFile;
    (void)flags;
    int rc = fsync(p->fd);
    VT("sync fd=%d rc=%d errno=%d", p->fd, rc, errno);
    if (rc != 0 && errno != ENOSYS) return SQLITE_IOERR_FSYNC;
    return SQLITE_OK;
}

static int espFileSize(sqlite3_file *pFile, sqlite3_int64 *pSize) {
    EspFile *p = (EspFile *)pFile;
    struct stat st;
    if (fstat(p->fd, &st) == 0) { *pSize = st.st_size; VT("filesize fd=%d -> %lld (fstat)", p->fd, (long long)*pSize); return SQLITE_OK; }
    off_t end = lseek(p->fd, 0, SEEK_END);   // fallback if fstat unsupported
    if (end < 0) { VT("filesize fd=%d FSTAT+lseek fail errno=%d", p->fd, errno); return SQLITE_IOERR_FSTAT; }
    *pSize = end;
    VT("filesize fd=%d -> %lld (lseek)", p->fd, (long long)*pSize);
    return SQLITE_OK;
}

// No concurrent access — locking is a formality (see file header).
static int espLock(sqlite3_file *p, int e)              { (void)p; (void)e; return SQLITE_OK; }
static int espUnlock(sqlite3_file *p, int e)            { (void)p; (void)e; return SQLITE_OK; }
static int espCheckReservedLock(sqlite3_file *p, int *r){ (void)p; *r = 0; return SQLITE_OK; }
static int espFileControl(sqlite3_file *p, int op, void *a){ (void)p; (void)op; (void)a; return SQLITE_NOTFOUND; }
static int espSectorSize(sqlite3_file *p)               { (void)p; return 512; }
static int espDeviceCharacteristics(sqlite3_file *p)    { (void)p; return 0; }

static const sqlite3_io_methods espIoMethods = {
    1,
    espClose, espRead, espWrite, espTruncate, espSync, espFileSize,
    espLock, espUnlock, espCheckReservedLock, espFileControl,
    espSectorSize, espDeviceCharacteristics,
};

static int espOpen(sqlite3_vfs *vfs, const char *name, sqlite3_file *pFile,
                   int flags, int *pOutFlags) {
    (void)vfs;
    EspFile *p = (EspFile *)pFile;
    memset(p, 0, sizeof(*p));
    int oflags = 0;
    if (flags & SQLITE_OPEN_READWRITE) oflags |= O_RDWR;
    else                               oflags |= O_RDONLY;
    if (flags & SQLITE_OPEN_CREATE)    oflags |= O_CREAT;
    if (flags & SQLITE_OPEN_EXCLUSIVE) oflags |= O_EXCL;
    p->fd = open(name, oflags, 0666);
    VT("open '%s' sqflags=0x%x oflags=0x%x -> fd=%d errno=%d", name, flags, oflags, p->fd, errno);
    if (p->fd < 0) return SQLITE_CANTOPEN;
    p->base.pMethods = &espIoMethods;
    if (pOutFlags) *pOutFlags = flags;
    return SQLITE_OK;
}

static int espDelete(sqlite3_vfs *vfs, const char *name, int syncDir) {
    (void)vfs; (void)syncDir;
    int rc = unlink(name);
    if (rc != 0 && errno == ENOENT) return SQLITE_OK;
    return rc == 0 ? SQLITE_OK : SQLITE_IOERR_DELETE;
}

static int espAccess(sqlite3_vfs *vfs, const char *name, int flags, int *pResOut) {
    (void)vfs; (void)flags;
    struct stat st;
    *pResOut = (stat(name, &st) == 0);
    return SQLITE_OK;
}

static int espFullPathname(sqlite3_vfs *vfs, const char *name, int nOut, char *zOut) {
    (void)vfs;
    // Paths are already absolute (/sdcard/...); just copy.
    sqlite3_snprintf(nOut, zOut, "%s", name);
    return SQLITE_OK;
}

static int espRandomness(sqlite3_vfs *vfs, int nByte, char *zOut) {
    (void)vfs;
    esp_fill_random(zOut, nByte);
    return nByte;
}

static int espSleep(sqlite3_vfs *vfs, int microseconds) {
    (void)vfs;
    int ms = (microseconds + 999) / 1000;
    vTaskDelay(pdMS_TO_TICKS(ms > 0 ? ms : 1));
    return microseconds;
}

static int espCurrentTime(sqlite3_vfs *vfs, double *pTime) {
    (void)vfs;
    time_t t = time(NULL);              // 0 if clock unsynced — harmless for the spike
    *pTime = 2440587.5 + (double)t / 86400.0;
    return SQLITE_OK;
}

static int espGetLastError(sqlite3_vfs *vfs, int nBuf, char *zBuf) {
    (void)vfs; (void)nBuf; (void)zBuf;
    return 0;
}

static sqlite3_vfs esp_fatfs_vfs = {
    1,                       // iVersion
    sizeof(EspFile),         // szOsFile
    ESPVFS_MAXPATH,          // mxPathname
    0,                       // pNext
    "esp-fatfs",             // zName
    0,                       // pAppData
    espOpen, espDelete, espAccess, espFullPathname,
    0, 0, 0, 0,              // dlopen/error/sym/close (OMIT_LOAD_EXTENSION)
    espRandomness, espSleep, espCurrentTime, espGetLastError,
};

int sqlite3_os_init(void) {
    return sqlite3_vfs_register(&esp_fatfs_vfs, 1 /* make default */);
}

int sqlite3_os_end(void) {
    return SQLITE_OK;
}
