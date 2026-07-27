/* The only script in the operator UI, and it exists for one page.
 *
 * Purging deletes files that cannot be recovered, so the storage page gets a
 * running total of what is selected and one confirmation step. Everything else
 * in this app is a plain form on purpose: the pages must keep working if this
 * file never loads.
 */
(function () {
  "use strict";

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
