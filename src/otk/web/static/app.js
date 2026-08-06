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

  // --- board drag and drop -------------------------------------------------
  // Moves the card first and reconciles with the server after: a status change
  // that takes a round trip to appear feels broken. On failure the card goes
  // back where it came from and says so, rather than lying about the state.
  (function () {
    var board = document.querySelector("[data-board]");
    if (!board) return;
    var csrf = board.dataset.csrf;
    var dragged = null;

    board.addEventListener("dragstart", function (event) {
      var card = event.target.closest("[data-ticket]");
      if (!card) return;
      dragged = card;
      card.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      // Firefox refuses to start a drag unless something is set.
      event.dataTransfer.setData("text/plain", card.dataset.ticket);
    });

    board.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-dragging");
      board.querySelectorAll(".is-over").forEach(function (z) {
        z.classList.remove("is-over");
      });
      dragged = null;
    });

    board.addEventListener("dragover", function (event) {
      var zone = event.target.closest("[data-dropzone]");
      if (!zone || !dragged) return;
      event.preventDefault();          // without this the drop never fires
      event.dataTransfer.dropEffect = "move";
      zone.classList.add("is-over");
    });

    board.addEventListener("dragleave", function (event) {
      var zone = event.target.closest("[data-dropzone]");
      if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("is-over");
    });

    board.addEventListener("drop", function (event) {
      var zone = event.target.closest("[data-dropzone]");
      if (!zone || !dragged) return;
      event.preventDefault();
      zone.classList.remove("is-over");

      var card = dragged;
      var column = zone.closest("[data-status]");
      var status = column.dataset.status;
      var from = card.parentNode;
      if (from === zone) return;

      var placeholder = card.nextSibling;
      zone.appendChild(card);
      var empty = zone.querySelector(".card-empty");
      if (empty) empty.remove();
      recount();

      var body = new FormData();
      body.append("status", status);
      body.append("csrf_token", csrf);
      fetch("/tickets/" + card.dataset.ticket + "/move", {
        method: "POST",
        body: body,
        credentials: "same-origin",
      })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
        })
        .catch(function (err) {
          from.insertBefore(card, placeholder);
          recount();
          window.alert(
            "Could not move " + card.dataset.ticket + " to " + status + ".\n" +
            err.message + "\n\nThe card has been put back. Reload to be sure."
          );
        });
    });

    function recount() {
      board.querySelectorAll("[data-status]").forEach(function (column) {
        var zone = column.querySelector("[data-dropzone]");
        var n = zone.querySelectorAll("[data-ticket]").length;
        var out = column.querySelector(".board-count");
        if (out) out.textContent = String(n);
        if (!n && !zone.querySelector(".card-empty")) {
          var li = document.createElement("li");
          li.className = "card-empty";
          li.textContent = "nothing here";
          zone.appendChild(li);
        }
      });
    }
  })();

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
