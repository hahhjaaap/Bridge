"use strict";
const status = document.getElementById("status");
for (const type of ["mark", "unmark"]) {
  document.getElementById(type).addEventListener("click", async () => {
    status.textContent = "Đang xử lý…";
    try {
      const reply = await chrome.runtime.sendMessage({type: "udb." + type});
      if (!reply?.ok) throw new Error(reply?.error || "Thao tác không thành công");
      status.textContent = type === "mark" ? "Đã đánh dấu UDB • AI" : "Đã bỏ đánh dấu";
    } catch (error) {
      status.textContent = String(error.message || error);
    }
  });
}
