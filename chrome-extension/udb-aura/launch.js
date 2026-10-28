"use strict";
const status = document.getElementById("status");
(async () => {
  let url;
  try {
    url = decodeURIComponent(location.hash.slice(1));
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("URL không hợp lệ");
  } catch {
    status.textContent = "Địa chỉ UDB không hợp lệ. Không mở tab.";
    return;
  }
  try {
    const reply = await chrome.runtime.sendMessage({type: "udb.launch", url});
    if (!reply?.ok) throw new Error(reply?.error || "Không thể tạo tab");
    const tab = await chrome.tabs.getCurrent();
    if (tab?.id != null) await chrome.tabs.remove(tab.id);
  } catch (error) {
    status.textContent = "UDB Aura: " + String(error.message || error);
  }
})();
