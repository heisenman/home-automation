// Web Push (client) — subscribe/unsubscribe the browser to background alert notifications.
// Payload-less: the server sends an empty "tickle"; the service worker (sw.js) fetches /api/v1/alerts
// and renders the notification. Here we only manage the PushSubscription + tell the server about it.

function urlB64ToU8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

export function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

// 'unsupported' | 'denied' | 'subscribed' | 'default'
export async function pushState() {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  const sub = reg && (await reg.pushManager.getSubscription());
  return sub ? "subscribed" : "default";
}

export async function enablePush() {
  if (!pushSupported()) return "unsupported";
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return perm === "denied" ? "denied" : "default";
  const keyResp = await fetch("/api/v1/push/vapid-public-key");
  if (!keyResp.ok) throw new Error("push not configured on server");
  const { key } = await keyResp.json();
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlB64ToU8(key),
  });
  const r = await fetch("/api/v1/push/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub),
  });
  if (!r.ok) throw new Error("server rejected subscription");
  return "subscribed";
}

export async function disablePush() {
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  const sub = reg && (await reg.pushManager.getSubscription());
  if (sub) {
    await fetch("/api/v1/push/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
  }
  return "default";
}
