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

// --- Shop: side cart, toast, image gallery, image upload preview ---------------------------
// Everything here is an enhancement. Without JavaScript the same forms post normally and the
// server redirects to the full cart page.
document.addEventListener("DOMContentLoaded", function () {
  var drawer = document.getElementById("cart-drawer");
  var backdrop = document.querySelector(".drawer-backdrop");
  var content = document.getElementById("cart-drawer-content");
  var toastEl = document.getElementById("toast");
  var lastFocus = null;
  var toastTimer = null;

  function toast(message, kind) {
    if (!toastEl) { return; }
    toastEl.textContent = message;
    toastEl.className = "toast toast-" + (kind || "info");
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 5000);
  }

  function setCount(count) {
    document.querySelectorAll("[data-cart-count]").forEach(function (badge) {
      badge.innerHTML = '<span class="visually-hidden">Items in cart: </span>' + count;
      badge.hidden = !count;
    });
  }

  function render(data) {
    content.innerHTML = data.html;
    setCount(data.count);
  }

  function isOpen() { return drawer && !drawer.hidden; }

  function openDrawer() {
    if (!drawer || isOpen()) { return; }
    lastFocus = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("drawer-open");
    requestAnimationFrame(function () {   // lets the slide-in transition run
      drawer.classList.add("is-open");
      backdrop.classList.add("is-open");
    });
    drawer.focus();
  }

  function closeDrawer() {
    if (!isOpen()) { return; }
    drawer.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    document.body.classList.remove("drawer-open");
    setTimeout(function () { drawer.hidden = true; backdrop.hidden = true; }, 220);
    if (lastFocus && document.contains(lastFocus)) { lastFocus.focus(); }
  }

  // The drawer answers with JSON: { ok, message, count, html }. Anything else (for example a
  // redirect to the sign-in page after the session ended) throws, and the caller falls back.
  function cartRequest(url, form) {
    var options = { headers: { "X-Requested-With": "fetch", "Accept": "application/json" }, credentials: "same-origin" };
    if (form) { options.method = "POST"; options.body = new FormData(form); }
    return fetch(url, options).then(function (response) {
      if ((response.headers.get("content-type") || "").indexOf("application/json") === -1) {
        throw new Error("unexpected response");
      }
      return response.json();
    });
  }

  // 5. "Add to cart": add in the background, then slide the cart in. The page stays where it is.
  document.querySelectorAll("form[data-cart-add]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!drawer) { return; }
      event.preventDefault();
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      cartRequest(form.action, form).then(function (data) {
        if (data.ok) { render(data); openDrawer(); } else { toast(data.message, "error"); }
      }).catch(function () {
        form.submit();              // no JSON came back: do it the classic way
      }).then(function () {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      });
    });
  });

  if (!drawer) { return; }

  // Open the side cart from the header link (the link still goes to /cart without JavaScript).
  document.querySelectorAll("[data-open-cart]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || link.getAttribute("aria-current") === "page") { return; }
      event.preventDefault();
      cartRequest("/cart/drawer").then(function (data) { render(data); openDrawer(); })
        .catch(function () { window.location.href = link.href; });
    });
  });

  // Close buttons are re-rendered with the drawer, so listen on the document.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-drawer-close]")) { closeDrawer(); }
  });
  document.addEventListener("keydown", function (event) {
    if (!isOpen()) { return; }
    if (event.key === "Escape") { closeDrawer(); return; }
    if (event.key === "Tab") {        // keep keyboard focus inside the open dialog
      var focusable = drawer.querySelectorAll("a[href], button:not([disabled]), input:not([disabled])");
      if (!focusable.length) { event.preventDefault(); drawer.focus(); return; }
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  // Quantity and Remove inside the drawer.
  content.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-cart-form]");
    if (!form) { return; }
    event.preventDefault();
    cartRequest(form.action, form).then(function (data) { render(data); drawer.focus(); })
      .catch(function () { form.submit(); });
  });
  content.addEventListener("change", function (event) {
    var form = event.target.closest("form[data-autosubmit]");
    if (form && form.checkValidity()) { form.requestSubmit(); }
  });
});

document.addEventListener("DOMContentLoaded", function () {
  // 6. Product gallery: clicking a thumbnail swaps the large picture (the link opens the file without JavaScript).
  var main = document.getElementById("gallery-main");
  document.querySelectorAll("[data-gallery-thumb]").forEach(function (thumb) {
    thumb.addEventListener("click", function (event) {
      if (!main) { return; }
      event.preventDefault();
      main.src = thumb.href;
      main.alt = thumb.dataset.alt;
      document.querySelectorAll("[data-gallery-thumb]").forEach(function (other) { other.removeAttribute("aria-current"); });
      thumb.setAttribute("aria-current", "true");
    });
  });

  // 7. Product form: preview chosen images and warn before upload about count and size.
  var input = document.getElementById("images");
  if (input) {
    var feedback = document.getElementById("images-feedback");
    var previews = document.getElementById("image-previews");
    var max = Number(input.dataset.maxImages);
    var existing = Number(input.dataset.existing) || 0;
    if (!(max > 0)) { return; }   // no limit information on the page: leave checking to the server
    var removeBoxes = document.querySelectorAll('input[name="remove_image"]');

    function check() {
      var removing = Array.prototype.filter.call(removeBoxes, function (box) { return box.checked; }).length;
      var room = max - (existing - removing);
      var files = Array.prototype.slice.call(input.files);
      var tooBig = files.filter(function (file) { return file.size > 2 * 1024 * 1024; });
      var problem = "";
      if (files.length > room) { problem = "You can add " + room + " more image" + (room === 1 ? "" : "s") + " (limit " + max + " per product)."; }
      else if (tooBig.length) { problem = tooBig[0].name + " is larger than 2 MB."; }
      input.setCustomValidity(problem);
      feedback.textContent = problem || (files.length ? files.length + " image" + (files.length === 1 ? "" : "s") + " selected." : "");
      feedback.className = "field-feedback " + (problem ? "is-invalid" : "is-valid");
      previews.innerHTML = "";
      files.forEach(function (file, index) {
        var item = document.createElement("li");
        var img = document.createElement("img");
        img.src = URL.createObjectURL(file);
        img.alt = "Preview of " + file.name;
        item.appendChild(img);
        previews.appendChild(item);
      });
    }
    input.addEventListener("change", check);
    removeBoxes.forEach(function (box) { box.addEventListener("change", check); });
  }
});

// 8. Weather: if the server could not tell where the visitor is (for example behind a hosting
// proxy), ask the visitor's own browser. It calls the same free services directly, so the answer
// is about the visitor's own connection. On failure it says why, and the search form stays usable.
document.addEventListener("DOMContentLoaded", function () {
  var bar = document.querySelector("[data-browser-locate]");
  if (!bar || !window.fetch) { return; }
  var status = bar.querySelector("[data-locate-status]");
  var form = bar.querySelector("[data-browser-location-form]");
  var providers = [
    { name: "ipwho.is", url: "https://ipwho.is/" },
    { name: "geojs.io", url: "https://get.geojs.io/v1/ip/geo.json" }
  ];
  var problems = [];

  function usable(data) {
    if (!data || data.success === false || !data.city) { return null; }
    var lat = parseFloat(data.latitude), lon = parseFloat(data.longitude);
    if (!isFinite(lat) || !isFinite(lon)) { return null; }
    return { city: data.city, country: data.country_code || "", lat: lat, lon: lon };
  }

  function giveUp() {
    status.textContent = "We could not work out your location automatically. Please search for your city below.";
    var why = bar.querySelector("[data-locate-reason]");
    var browserWhy = " Your browser could not reach the location services either (" + problems.join("; ") +
      "). An ad-blocker or privacy setting can cause this.";
    if (why) { why.textContent += browserWhy; }
    else {
      var p = document.createElement("p");
      p.className = "location-reason";
      p.innerHTML = "<strong>Why:</strong> ";
      p.appendChild(document.createTextNode(browserWhy.trim()));
      status.parentNode.appendChild(p);
    }
  }

  function tryProvider(index) {
    if (index >= providers.length) { giveUp(); return; }
    var provider = providers[index];
    var controller = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) { controller.abort(); } }, 6000);
    fetch(provider.url, controller ? { signal: controller.signal } : {})
      .then(function (response) {
        if (!response.ok) { throw new Error("HTTP " + response.status); }
        return response.json();
      })
      .then(function (data) {
        clearTimeout(timer);
        var place = usable(data);
        if (!place) { throw new Error("no city for this address"); }
        status.textContent = "Found " + place.city + ". Loading its weather…";
        form.elements.city.value = place.city;
        form.elements.country.value = place.country;
        form.elements.lat.value = place.lat;
        form.elements.lon.value = place.lon;
        form.submit();
      })
      .catch(function (error) {
        clearTimeout(timer);
        problems.push(provider.name + ": " + (error.name === "AbortError" ? "timed out" : (error.message || "blocked")));
        tryProvider(index + 1);
      });
  }

  tryProvider(0);
});

// 9. Confirmation code page: only digits, submit as soon as 6 are typed, and count down the resend wait.
document.addEventListener("DOMContentLoaded", function () {
  var code = document.getElementById("code");
  if (code) {
    code.addEventListener("input", function () {
      code.value = code.value.replace(/\D+/g, "").slice(0, 6);
      if (code.value.length === 6) { code.form.requestSubmit(); }
    });
  }
  var resend = document.querySelector("button[data-wait]");
  if (resend) {
    var left = Number(resend.dataset.wait);
    resend.disabled = true;
    var timer = setInterval(function () {
      left -= 1;
      if (left <= 0) { clearInterval(timer); resend.disabled = false; resend.textContent = "Send a new code"; }
      else { resend.textContent = "Send a new code (in " + left + "s)"; }
    }, 1000);
  }
});

// 10. Mobile menu: the Menu button opens and closes the header links (they stay visible without JavaScript).
document.addEventListener("DOMContentLoaded", function () {
  var button = document.querySelector(".nav-toggle");
  var nav = document.getElementById("site-nav");
  if (!button || !nav) { return; }
  button.hidden = false;
  var label = button.querySelector("span");
  var icon = button.querySelector("use");

  function setOpen(open) {
    nav.classList.toggle("is-open", open);
    button.setAttribute("aria-expanded", String(open));
    label.textContent = open ? "Close" : "Menu";
    icon.setAttribute("href", open ? "#i-x" : "#i-menu");
  }
  button.addEventListener("click", function () { setOpen(button.getAttribute("aria-expanded") !== "true"); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && nav.classList.contains("is-open")) { setOpen(false); button.focus(); }
  });
  document.addEventListener("click", function (event) {      // a tap outside the menu closes it
    if (nav.classList.contains("is-open") && !event.target.closest(".site-header")) { setOpen(false); }
  });
  window.matchMedia("(min-width: 861px)").addEventListener("change", function () { setOpen(false); });
});
