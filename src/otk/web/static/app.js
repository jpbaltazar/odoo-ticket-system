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
    // Zoom steps, as a share of the viewport width. `null` is fit-to-screen.
    // Expressed against the viewport rather than the image's natural size so
    // a step always changes what you see — a 900px screenshot and a 3840px one
    // both start out fitted, and 2x fitted is 2x either way.
    var STEPS = [null, 2, 4];
    var overlay = null;
    var image = null;
    var step = 0;
    var drag = null;

    function close() {
      if (!overlay) return;
      overlay.remove();
      overlay = null;
      image = null;
      drag = null;
      document.removeEventListener("keydown", onKey);
    }

    function applyStep() {
      // Classes rather than inline styles: the CSP has no 'unsafe-inline'.
      overlay.classList.toggle("is-zoomed", STEPS[step] === 2);
      overlay.classList.toggle("is-zoomed-max", STEPS[step] === 4);
    }

    function setStep(next, anchor) {
      // Keep whatever was under the cursor under the cursor. Without this,
      // zooming always lands you back in the middle of the image.
      var rect = image.getBoundingClientRect();
      var fx = rect.width ? ((anchor ? anchor.clientX : innerWidth / 2) - rect.left) / rect.width : .5;
      var fy = rect.height ? ((anchor ? anchor.clientY : innerHeight / 2) - rect.top) / rect.height : .5;

      step = (next + STEPS.length) % STEPS.length;
      applyStep();

      var grown = image.getBoundingClientRect();
      overlay.scrollLeft += grown.width * fx - (rect.width * fx + (rect.left - grown.left));
      overlay.scrollTop += grown.height * fy - (rect.height * fy + (rect.top - grown.top));
    }

    function onKey(event) {
      if (event.key === "Escape") close();
      else if (event.key === "+" || event.key === "=") setStep(step + 1, null);
      else if (event.key === "-") setStep(step - 1, null);
    }

    function open(href, caption) {
      close();
      step = 0;
      overlay = document.createElement("div");
      overlay.className = "lightbox";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-label", caption || "Screenshot");

      var stage = document.createElement("div");
      stage.className = "lightbox-stage";
      image = document.createElement("img");
      image.src = href;
      image.alt = caption || "";
      image.draggable = false;   // else the browser starts an image drag instead
      stage.appendChild(image);
      overlay.appendChild(stage);

      var button = document.createElement("button");
      button.type = "button";
      button.className = "lightbox-close";
      button.textContent = "Close";
      button.addEventListener("click", close);
      overlay.appendChild(button);

      var label = document.createElement("p");
      label.className = "lightbox-caption";
      label.textContent = (caption ? caption + " — " : "") +
        "click the image to zoom, drag to pan, Esc to close";
      overlay.appendChild(label);

      // Click the image to step through the zoom levels; click the backdrop
      // around it to close. A drag that ends on the image is a pan, not a
      // click, so it must not also change the zoom.
      image.addEventListener("click", function (event) {
        event.stopPropagation();
        if (drag && drag.moved) return;
        setStep(step + 1, event);
      });
      image.addEventListener("mousedown", function (event) {
        if (event.button !== 0) return;
        event.preventDefault();
        drag = {x: event.clientX, y: event.clientY,
                left: overlay.scrollLeft, top: overlay.scrollTop, moved: false};
        overlay.classList.add("is-panning");
      });
      overlay.addEventListener("wheel", function (event) {
        if (!event.ctrlKey) return;   // plain wheel still scrolls a zoomed image
        event.preventDefault();
        setStep(step + (event.deltaY < 0 ? 1 : -1), event);
      }, {passive: false});
      overlay.addEventListener("click", close);

      document.body.appendChild(overlay);
      button.focus();
      document.addEventListener("keydown", onKey);
    }

    document.addEventListener("mousemove", function (event) {
      if (!drag) return;
      var dx = event.clientX - drag.x;
      var dy = event.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;   // a shaky click
      drag.moved = true;
      overlay.scrollLeft = drag.left - dx;
      overlay.scrollTop = drag.top - dy;
    });

    document.addEventListener("mouseup", function () {
      if (!drag) return;
      overlay.classList.remove("is-panning");
      // Cleared after the click event that follows this mouseup has been seen.
      var finished = drag;
      setTimeout(function () { if (drag === finished) drag = null; }, 0);
    });

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
