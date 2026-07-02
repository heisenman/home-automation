// Force-included (-include) ahead of sqlite3.c ONLY (see CMakeLists).
//
// Why: this project sets CONFIG_COMPILER_ASSERT_NDEBUG_EVALUATE=y, so IDF's
// assert.h defines `assert(e)` as `((void)(e))` even under NDEBUG — it still
// *evaluates* the expression. SQLite's release build assumes NDEBUG makes
// assert() vanish entirely; many of its asserts reference symbols that only
// exist under SQLITE_DEBUG, so an evaluating assert fails to compile.
//
// Trick: pull IDF's assert.h now (it is `#pragma once`, so this fires its
// include-guard), THEN override assert to a genuine no-op. When sqlite3.c later
// does `#include <assert.h>`, the pragma-once skips it and our definition wins.
// Fully local to this component — no global sdkconfig change. SQLite asserts are
// documented side-effect-free, so dropping them is the standard release build.
#pragma once
#include <assert.h>
#undef assert
#define assert(__e) ((void)0)
