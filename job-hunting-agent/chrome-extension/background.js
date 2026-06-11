let ws = null;
let connected = false;
let activeTask = "Awaiting instructions...";
let lastApplied = "None (Idle)";
let wsUrl = "ws://127.0.0.1:8000/ws/extension"; // FastAPI WebSocket server endpoint

function connectWebSocket() {
  if (ws) {
    ws.close();
  }

  console.log("Connecting to WebSocket backend...");
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    connected = true;
    activeTask = "Listening for agent instructions...";
    console.log("WebSocket connected to agent platform!");
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("Received backend directive:", data);
      
      if (data.type === "APPLY_JOB") {
        activeTask = `Applying to ${data.role} at ${data.company}`;
        // Open the application page in a background/pinned tab
        chrome.tabs.create({ url: data.url, active: true }, (tab) => {
          // Keep track of the tab id and details to orchestrate via content script
          chrome.storage.local.set({
            currentApplication: {
              tabId: tab.id,
              userData: data.userData,
              resumeV1Text: data.resumeV1Text
            }
          });
        });
      }
    } catch (err) {
      console.error("Failed to parse message:", err);
    }
  };

  ws.onclose = () => {
    connected = false;
    activeTask = "Awaiting connection...";
    console.log("WebSocket connection closed.");
    // Attempt auto-reconnection in 5 seconds
    setTimeout(connectWebSocket, 5000);
  };

  ws.onerror = (err) => {
    console.error("WebSocket error:", err);
  };
}

// Automatically connect on service worker startup
connectWebSocket();

// Handle messages from the extension popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "getStatus") {
    sendResponse({
      connected: connected,
      activeTask: activeTask,
      lastApplied: lastApplied
    });
  } else if (request.action === "toggleConnect") {
    if (connected) {
      if (ws) ws.close();
    } else {
      connectWebSocket();
    }
    sendResponse({ success: true });
  } else if (request.action === "syncCookies") {
    chrome.cookies.getAll({ url: "https://www.linkedin.com" }, (cookies) => {
      if (chrome.runtime.lastError || !cookies || cookies.length === 0) {
        console.error("Failed to retrieve LinkedIn cookies:", chrome.runtime.lastError);
        sendResponse({ success: false });
        return;
      }
      
      console.log(`Retrieved ${cookies.length} cookies from LinkedIn. Syncing...`);
      fetch("http://127.0.0.1:8000/api/cookies", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ cookies: cookies })
      })
      .then(res => {
        if (res.ok) {
          console.log("LinkedIn cookies synchronized successfully to Gateway!");
          sendResponse({ success: true });
        } else {
          sendResponse({ success: false });
        }
      })
      .catch(err => {
        console.error("Error posting cookies to gateway:", err);
        sendResponse({ success: false });
      });
    });
    return true; // Keep message channel open for async response
  } else if (request.action === "jobApplied") {
    // Content script notifies backend that the application is submitted
    lastApplied = `${request.role} at ${request.company} (Success)`;
    activeTask = "Listening for agent instructions...";
    
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "APPLICATION_RESULT",
        status: "Submitted",
        jobId: request.jobId,
        company: request.company,
        role: request.role
      }));
    }
    sendResponse({ success: true });
  }
});
