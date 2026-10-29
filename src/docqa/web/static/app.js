/* A classic bootstrap can report a failed ES module instead of leaving a dead UI. */
(() => {
  let bound = false;
  const banner = document.getElementById("status-banner");
  const failed = () => {
    if (bound) return;
    document.documentElement.dataset.appState = "failed";
    banner.hidden = false;
    banner.classList.add("error");
    banner.textContent =
      "交互组件加载失败，上传和聊天尚未启用。请重启 Web 服务并按 Ctrl+F5 刷新；检查静态 JS 是否返回 JavaScript 类型。";
  };
  // Prevent a native GET navigation from discarding typed text during startup.
  document.getElementById("question-form").addEventListener(
    "submit",
    (event) => {
      if (!bound) {
        event.preventDefault();
        failed();
      }
    },
    true,
  );
  window.addEventListener(
    "docqa:bound",
    () => {
      bound = true;
      document.documentElement.dataset.appState = "bound";
    },
    { once: true },
  );
  window.addEventListener("unhandledrejection", failed);
  import("./js/main.js").catch(failed);
})();
