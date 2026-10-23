// Small usability enhancements. Every form still works without JavaScript,
// and the Flask routes repeat all validation on the server.

document.addEventListener("DOMContentLoaded", function () {
  // 1. Show / hide password buttons.
  document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
    var input = document.getElementById(button.dataset.togglePassword);
    button.hidden = false;
    button.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.textContent = showing ? "Show" : "Hide";
      button.setAttribute("aria-pressed", String(!showing));
    });
  });

  // 2. Tell users straight away whether "Confirm password" matches.
  document.querySelectorAll("input[data-match]").forEach(function (confirmInput) {
    var original = document.getElementById(confirmInput.dataset.match);
    var feedback = document.getElementById(confirmInput.id + "-feedback");

    function check() {
      if (!confirmInput.value) {
        confirmInput.setCustomValidity("");
        feedback.textContent = "";
        feedback.className = "field-feedback";
        return;
      }
      var matches = confirmInput.value === original.value;
      confirmInput.setCustomValidity(matches ? "" : "Passwords do not match.");
      feedback.textContent = matches ? "Passwords match." : "Passwords do not match yet.";
      feedback.className = "field-feedback " + (matches ? "is-valid" : "is-invalid");
    }

    confirmInput.addEventListener("input", check);
    original.addEventListener("input", check);
  });

  // 4. Show a loading state while a weather search is running.
  document.querySelectorAll("form[data-loading-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity()) { return; }
      form.classList.add("is-loading");
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      button.innerHTML = button.dataset.loadingText;
    });
  });

  // 3. Ask the admin to confirm changes that affect someone's access.
  document.querySelectorAll("form[data-confirm-user]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var role = document.querySelector('select[name="role"][form="' + form.id + '"]');
      var status = document.querySelector('select[name="is_active"][form="' + form.id + '"]');
      var name = form.dataset.confirmUser;
      var changes = [];

      if (role.value !== role.dataset.initial) {
        changes.push("change their role from " + role.dataset.initial + " to " + role.value);
      }
      if (status.value === "0" && status.dataset.initial === "1") {
        changes.push("deactivate their account so they can no longer sign in");
      }
      if (changes.length && !window.confirm("Are you sure you want to " + changes.join(" and ") + " for " + name + "?")) {
        event.preventDefault();
      }
    });
  });
});

// Pages restored from the back/forward cache (e.g. a dashboard after signing out)
// are reloaded so the server can check the session again.
window.addEventListener("pageshow", (event) => {
  const nav = performance.getEntriesByType("navigation")[0];
  if (event.persisted || (nav && nav.type === "back_forward")) {
    window.location.reload();
  }
});

// A page restored from the back/forward cache must not stay stuck on "Searching...".
window.addEventListener("pageshow", function () {
  document.querySelectorAll("form.is-loading").forEach(function (form) {
    form.classList.remove("is-loading");
    var button = form.querySelector("button[type=submit]");
    button.disabled = false;
    button.textContent = "Get weather";
  });
});
