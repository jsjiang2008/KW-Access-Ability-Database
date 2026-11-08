const form = document.getElementById("login");
const button = document.getElementById("submit");
const error = document.getElementById("error");
let csrfToken = "";

function showError(message) {
  error.textContent = message;
  error.hidden = false;
}

async function prepare() {
  const response = await fetch("/api/session", { credentials: "same-origin" });
  if (!response.ok) throw new Error("Could not connect. Refresh the page to try again.");
  const data = await response.json();
  if (data.authenticated) { window.location.replace("/"); return; }
  csrfToken = data.csrfToken;
  button.disabled = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ username: form.username.value, password: form.password.value })
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 403) await prepare();
      throw new Error(data.error || "Sign-in failed. Please try again.");
    }
    form.password.value = "";
    window.location.replace("/");
  } catch (e) { showError(e.message); }
  finally { button.disabled = false; }
});

window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
prepare().catch((e) => showError(e.message));
