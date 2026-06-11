console.log("💼 Career Agent content script injected!");

// Helper to fill input fields by matching labels or placeholders
function fillFormFields(userData) {
  const inputs = document.querySelectorAll("input, textarea");
  
  inputs.forEach(input => {
    // Skip file inputs and buttons
    if (input.type === "submit" || input.type === "button" || input.type === "file") return;

    // Retrieve placeholder and name attributes to match
    const name = (input.name || "").toLowerCase();
    const placeholder = (input.placeholder || "").toLowerCase();
    const id = (input.id || "").toLowerCase();
    
    // Attempt to locate associated label text
    let labelText = "";
    if (input.id) {
      const label = document.querySelector(`label[for="${input.id}"]`);
      if (label) {
        labelText = label.innerText.toLowerCase();
      }
    }
    
    const combinedTerms = `${name} ${placeholder} ${id} ${labelText}`;

    // Fill logic matching keys
    if (combinedTerms.includes("first") || combinedTerms.includes("given")) {
      input.value = userData.first_name;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (combinedTerms.includes("last") || combinedTerms.includes("family") || combinedTerms.includes("surname")) {
      input.value = userData.last_name;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (combinedTerms.includes("email") || combinedTerms.includes("mail")) {
      input.value = userData.email;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (combinedTerms.includes("phone") || combinedTerms.includes("mobile") || combinedTerms.includes("contact")) {
      input.value = userData.phone;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
}

// Check if we are currently running an application task
chrome.storage.local.get("currentApplication", (data) => {
  if (data && data.currentApplication) {
    const app = data.currentApplication;
    console.log("Current active task details loaded:", app);

    // Give the page 1.5 seconds to settle down and render fields
    setTimeout(() => {
      fillFormFields(app.userData);
      console.log("Form fields filled successfully!");
      
      // Auto-upload pdf files requires user gesture or debugger permissions, 
      // but in content scripts we can highlight the file input so the user can easily drag and drop, 
      // or we can prompt them.
      const fileInput = document.querySelector("input[type='file']");
      if (fileInput) {
        fileInput.style.border = "3px dashed #6366f1";
        fileInput.style.padding = "10px";
        fileInput.style.background = "rgba(99, 102, 241, 0.1)";
        
        // Notify user visually
        const notifyDiv = document.createElement("div");
        notifyDiv.style.position = "fixed";
        notifyDiv.style.top = "10px";
        notifyDiv.style.right = "10px";
        notifyDiv.style.background = "#6366f1";
        notifyDiv.style.color = "white";
        notifyDiv.style.padding = "15px";
        notifyDiv.style.borderRadius = "8px";
        notifyDiv.style.zIndex = "999999";
        notifyDiv.style.fontWeight = "bold";
        notifyDiv.innerHTML = "💡 Career Agent filled the form! Please drop your resume in the highlighted box.";
        document.body.appendChild(notifyDiv);
      }

      // Check if we are in mock-form. If so, auto submit!
      if (window.location.href.includes("/mock-form")) {
        setTimeout(() => {
          const submitBtn = document.querySelector("button[type='submit']");
          if (submitBtn) {
            submitBtn.click();
            chrome.runtime.sendMessage({
              action: "jobApplied",
              role: "Software Engineer",
              company: "Mock Company",
              jobId: "mock-1"
            });
            chrome.storage.local.remove("currentApplication");
          }
        }, 3000);
      }
    }, 1500);
  }
});
