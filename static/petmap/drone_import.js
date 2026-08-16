/* Drone flight import — parse each photo's EXIF here, upload only coordinates.
 *
 * The form still posts multipart if this script never runs, which is the
 * no-JavaScript fallback the server also accepts. When it does run it takes
 * over the submit: a 300-frame sortie becomes a few kilobytes of JSON instead
 * of several gigabytes of imagery, which is what the 16 MB request cap was
 * rejecting with a 413.
 */
(function () {
  "use strict";

  var CFG = window.PETMAP_DETAIL;
  if (!CFG || !CFG.urls || !CFG.urls.drone) return;
  if (!window.PetMapExif || !window.FileReader) return;   // fall back to multipart

  var U = window.PetMapUtil;
  var form = document.getElementById("drone-form");
  if (!form) return;

  var input = document.getElementById("drone-photos");
  var notes = document.getElementById("drone-notes");
  var submit = document.getElementById("drone-submit");
  var status = document.getElementById("drone-status");

  function say(text, isError) {
    status.hidden = !text;
    status.textContent = text || "";
    status.classList.toggle("is-error", !!isError);
  }

  // Selecting files is the expensive-looking step to a user, so report the
  // count immediately rather than waiting for submit.
  input.addEventListener("change", function () {
    var n = input.files ? input.files.length : 0;
    say(n ? n + (n === 1 ? " photo selected." : " photos selected. Nothing is uploaded until you add the flight.") : "");
  });

  form.addEventListener("submit", function (event) {
    var files = input.files ? Array.prototype.slice.call(input.files) : [];
    if (!files.length) return;              // let the browser's required kick in

    event.preventDefault();
    submit.disabled = true;
    say("Reading photo locations on this device…");

    window.PetMapExif.readFiles(files, function (done, total) {
      say("Reading " + done + " of " + total + " — nothing has been uploaded.");
    }).then(function (result) {
      if (result.fixes.length < 2) {
        submit.disabled = false;
        say("Only " + result.fixes.length + " of " + files.length +
            " photos had GPS in their header, and a path needs at least two. " +
            "Check that geotagging was switched on for the flight.", true);
        return;
      }

      say("Sending " + result.fixes.length + " positions (" +
          Math.ceil(JSON.stringify(result.fixes).length / 1024) + " KB)…");

      return fetch(CFG.urls.drone, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": U.csrfToken() },
        body: JSON.stringify({ fixes: result.fixes, notes: notes ? notes.value : "" })
      }).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
          return data;
        });
      }).then(function (data) {
        say(data.message);
        window.location.reload();
      });
    }).catch(function (err) {
      submit.disabled = false;
      say(err.message || "Couldn't add that flight.", true);
    });
  });
})();
