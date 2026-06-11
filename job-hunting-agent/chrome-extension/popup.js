document.addEventListener('DOMContentLoaded', () => {
  const statusBadge = document.getElementById('status');
  const activeTask = document.getElementById('active-task');
  const lastApplied = document.getElementById('last-applied');
  const connectBtn = document.getElementById('connect-btn');

  // Request status updates from background script
  function updateUI() {
    chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
      if (chrome.runtime.lastError) {
        console.log("Runtime error:", chrome.runtime.lastError);
        return;
      }
      if (response) {
        if (response.connected) {
          statusBadge.textContent = "Connected";
          statusBadge.className = "status-badge connected";
          connectBtn.textContent = "Disconnect";
        } else {
          statusBadge.textContent = "Disconnected";
          statusBadge.className = "status-badge";
          connectBtn.textContent = "Connect Backend Console";
        }
        activeTask.textContent = response.activeTask || "Awaiting instructions...";
        lastApplied.textContent = response.lastApplied || "None (Idle)";
      }
    });
  }

  // Poll status occasionally when popup is open
  updateUI();
  const timer = setInterval(updateUI, 1000);

  connectBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: "toggleConnect" }, (response) => {
      updateUI();
    });
  });

  const syncBtn = document.getElementById('sync-cookies-btn');
  syncBtn.addEventListener('click', () => {
    syncBtn.textContent = "Syncing...";
    syncBtn.disabled = true;
    chrome.runtime.sendMessage({ action: "syncCookies" }, (response) => {
      syncBtn.disabled = false;
      if (response && response.success) {
        syncBtn.textContent = "Session Synced ✓";
        setTimeout(() => {
          syncBtn.textContent = "Sync LinkedIn Session";
        }, 3000);
      } else {
        syncBtn.textContent = "Sync Failed ✗";
        setTimeout(() => {
          syncBtn.textContent = "Sync LinkedIn Session";
        }, 3000);
      }
    });
  });

  // Clean up timer on close
  window.addEventListener('unload', () => {
    clearInterval(timer);
  });
});
