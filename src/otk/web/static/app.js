/* The only script in the operator UI.
 *
 * Two jobs: a running total and a confirmation on the purge page, because it
 * deletes files that cannot be recovered; and copy buttons on tracebacks,
 * which otherwise mean selecting several hundred lines by hand. Everything
 * else is a plain form on purpose — the pages must keep working if this file
 * never loads.
 */
(function () {
  "use strict";

  // --- zoom a screenshot ---------------------------------------------------
  // The thumbnail is capped so one screenshot cannot own the page; clicking it
  // opens full resolution over the ticket. The anchor still points at the file,
  // so without this script the click just opens it.
  (function () {
    var overlay = null;

    function close() {
      if (!overlay) return;
      overlay.remove();
      overlay = null;
      document.removeEventListener("keydown", onKey);
    }

    function onKey(event) {
      if (event.key === "Escape") close();
    }

    function open(href, caption) {
      close();
      overlay = document.createElement("div");
      overlay.className = "lightbox";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-label", caption || "Screenshot");

      var image = document.createElement("img");
      image.src = href;
      image.alt = caption || "";
      overlay.appendChild(image);

      var button = document.createElement("button");
      button.type = "button";
      button.className = "lightbox-close";
      button.textContent = "Close";
      overlay.appendChild(button);

      if (caption) {
        var label = document.createElement("p");
        label.className = "lightbox-caption";
        label.textContent = caption + " — click anywhere or press Esc to close";
        overlay.appendChild(label);
      }

      // Clicking the image itself should not close it; the backdrop should.
      image.addEventListener("click", function (event) { event.stopPropagation(); });
      overlay.addEventListener("click", close);
      document.body.appendChild(overlay);
      button.focus();
      document.addEventListener("keydown", onKey);
    }

    document.addEventListener("click", function (event) {
      var link = event.target.closest("a[data-lightbox]");
      if (!link) return;
      // Leave modified clicks alone so "open in new tab" still works.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      var img = link.querySelector("img");
      open(link.getAttribute("href"), img ? img.getAttribute("alt") : "");
    });
  })();

  // --- Ctrl+Enter sends a reply -------------------------------------------
  // The textarea is multi-line, so plain Enter has to keep inserting newlines.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    var area = event.target.closest("textarea");
    if (!area) return;
    var form = area.form;
    if (!form) return;
    event.preventDefault();
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  });

  // --- copy buttons -------------------------------------------------------
  // navigator.clipboard needs a secure context, so it is absent over plain
  // http on an IP. The textarea fallback keeps the button working there.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var scratch = document.createElement("textarea");
      scratch.value = text;
      // Off-screen rather than hidden: a display:none element cannot be selected.
      scratch.setAttribute("readonly", "");
      scratch.style.position = "fixed";
      scratch.style.top = "-1000px";
      document.body.appendChild(scratch);
      scratch.select();
      try {
        document.execCommand("copy") ? resolve() : reject(new Error("copy refused"));
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(scratch);
      }
    });
  }

  // Delegated, so it covers buttons inside a <details> that was closed at load.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    // The button sits inside <summary>, so without this the copy also toggles
    // the disclosure open or shut.
    event.preventDefault();

    var source = document.querySelector(button.getAttribute("data-copy") || "") ||
      (button.closest("details") || document).querySelector("pre");
    if (!source) return;

    var original = button.dataset.label || button.textContent;
    button.dataset.label = original;
    copyText(source.textContent).then(
      function () {
        button.textContent = "Copied";
        button.classList.add("is-copied");
      },
      function () {
        button.textContent = "Press Ctrl+C";
        var range = document.createRange();
        range.selectNodeContents(source);
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
    );
    window.setTimeout(function () {
      button.textContent = original;
      button.classList.remove("is-copied");
    }, 1600);
  });

  var form = document.getElementById("purge-form");
  if (!form) return;

  var boxes = Array.prototype.slice.call(
    form.querySelectorAll('input[name="ticket_id"]')
  );
  var all = document.getElementById("purge-all");
  var totalOut = document.getElementById("purge-total");
  var filesOut = document.getElementById("purge-files");

  // Mirrors the humanbytes Jinja filter: decimal units, one decimal place.
  function humanbytes(size) {
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (size >= 1000 && i < units.length - 1) {
      size /= 1000;
      i += 1;
    }
    return i === 0 ? size.toFixed(0) + " B" : size.toFixed(1) + " " + units[i];
  }

  function refresh() {
    var bytes = 0;
    var files = 0;
    var picked = 0;
    boxes.forEach(function (box) {
      if (!box.checked) return;
      picked += 1;
      bytes += parseInt(box.getAttribute("data-bytes"), 10) || 0;
      files += parseInt(box.getAttribute("data-files"), 10) || 0;
    });
    if (totalOut) totalOut.textContent = humanbytes(bytes);
    if (filesOut) filesOut.textContent = String(files);
    if (all) all.checked = picked > 0 && picked === boxes.length;
    form.dataset.selected = String(picked);
  }

  boxes.forEach(function (box) {
    box.addEventListener("change", refresh);
  });

  if (all) {
    all.addEventListener("change", function () {
      boxes.forEach(function (box) {
        box.checked = all.checked;
      });
      refresh();
    });
  }

  form.addEventListener("submit", function (event) {
    var picked = boxes.filter(function (box) {
      return box.checked;
    });
    if (!picked.length) {
      event.preventDefault();
      window.alert("Nothing is selected, so there is nothing to purge.");
      return;
    }
    var message =
      "Permanently delete the files of " +
      picked.length +
      " ticket(s)?\n\nThe ticket text and comments are kept. The files cannot be recovered.";
    if (!window.confirm(message)) event.preventDefault();
  });

  refresh();
})();
