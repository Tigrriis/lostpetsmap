/* Read GPS and capture time out of a JPEG, in the browser, without uploading it.
 *
 * Why this exists: a Matrice 4T sortie is a few hundred frames at 8-20 MB each.
 * Sending those to the server to read a dozen bytes of header from each was
 * costing 413s on anything past a handful, and would cost real money in
 * bandwidth if it worked. The coordinates are all we ever keep, so the parsing
 * belongs on the device — the photos then never leave it at all.
 *
 * Only the first slice of each file is read. EXIF lives in an APP1 segment near
 * the start of a JPEG, so a 20 MB photo costs a 128 KB read.
 *
 * Deliberately hand-rolled rather than a library: the whole job is one segment
 * and about six tags, and a CDN dependency on the report form is a worse trade
 * than the code below.
 */
window.PetMapExif = (function () {
  "use strict";

  var HEAD_BYTES = 131072;        // 128 KB — enough for APP1 in any normal JPEG
  var RETRY_BYTES = 1048576;      // 1 MB, if an offset pointed past the first slice

  // TIFF field types, by their size in bytes.
  var TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8 };

  var TAG_EXIF_IFD = 0x8769;
  var TAG_GPS_IFD = 0x8825;
  var TAG_DATETIME_ORIGINAL = 0x9003;
  var TAG_DATETIME = 0x0132;

  var GPS = {
    LAT_REF: 1, LAT: 2, LNG_REF: 3, LNG: 4, ALT_REF: 5, ALT: 6
  };

  /* Thrown when an offset points outside the slice we read, so the caller
     knows a bigger slice might succeed rather than the file being unreadable. */
  function OutOfRange() {}

  function findApp1(view) {
    if (view.getUint16(0) !== 0xFFD8) return -1;      // not a JPEG
    var offset = 2;
    while (offset + 4 <= view.byteLength) {
      var marker = view.getUint16(offset);
      if ((marker & 0xFF00) !== 0xFF00) return -1;     // desynchronised
      var size = view.getUint16(offset + 2);
      if (marker === 0xFFE1) {
        // "Exif\0\0" then the TIFF header.
        if (offset + 10 > view.byteLength) return -1;
        if (view.getUint32(offset + 4) !== 0x45786966) return -1;   // "Exif"
        return offset + 10;
      }
      if (marker === 0xFFDA) return -1;                // start of scan; no EXIF
      offset += 2 + size;
    }
    return -1;
  }

  function readIfd(view, tiff, ifdOffset, little) {
    var at = tiff + ifdOffset;
    if (at + 2 > view.byteLength) throw new OutOfRange();
    var count = view.getUint16(at, little);
    var entries = {};
    for (var i = 0; i < count; i++) {
      var e = at + 2 + i * 12;
      if (e + 12 > view.byteLength) throw new OutOfRange();
      var tag = view.getUint16(e, little);
      var type = view.getUint16(e + 2, little);
      var num = view.getUint32(e + 4, little);
      var size = (TYPE_SIZE[type] || 0) * num;
      var valueAt = size > 4 ? tiff + view.getUint32(e + 8, little) : e + 8;
      entries[tag] = { type: type, count: num, at: valueAt };
    }
    return entries;
  }

  function rational(view, at, little, signed) {
    if (at + 8 > view.byteLength) throw new OutOfRange();
    var n = signed ? view.getInt32(at, little) : view.getUint32(at, little);
    var d = signed ? view.getInt32(at + 4, little) : view.getUint32(at + 4, little);
    return d ? n / d : 0;
  }

  function ascii(view, entry) {
    if (!entry || entry.type !== 2) return null;
    var end = entry.at + entry.count;
    if (end > view.byteLength) throw new OutOfRange();
    var out = "";
    for (var i = entry.at; i < end; i++) {
      var c = view.getUint8(i);
      if (c === 0) break;
      out += String.fromCharCode(c);
    }
    return out;
  }

  function degrees(view, entry, ref, little) {
    // Three RATIONALs: degrees, minutes, seconds.
    if (!entry || entry.count < 3) return null;
    var d = rational(view, entry.at, little);
    var m = rational(view, entry.at + 8, little);
    var s = rational(view, entry.at + 16, little);
    var value = d + m / 60 + s / 3600;
    if (ref && (ref.toUpperCase() === "S" || ref.toUpperCase() === "W")) value = -value;
    return value;
  }

  function parse(buffer) {
    var view = new DataView(buffer);
    var tiff = findApp1(view);
    if (tiff < 0) return null;
    if (tiff + 8 > view.byteLength) throw new OutOfRange();

    var endian = view.getUint16(tiff);
    if (endian !== 0x4949 && endian !== 0x4D4D) return null;
    var little = endian === 0x4949;
    if (view.getUint16(tiff + 2, little) !== 0x002A) return null;

    var ifd0 = readIfd(view, tiff, view.getUint32(tiff + 4, little), little);

    // Capture time. DateTimeOriginal belongs in the Exif sub-IFD and that is
    // where a DJI file puts it, but plenty of writers leave it in IFD0
    // instead, so check both before falling back to IFD0's DateTime.
    var taken = null;
    if (ifd0[TAG_EXIF_IFD]) {
      var exif = readIfd(view, tiff, view.getUint32(ifd0[TAG_EXIF_IFD].at, little), little);
      taken = ascii(view, exif[TAG_DATETIME_ORIGINAL]);
    }
    if (!taken) taken = ascii(view, ifd0[TAG_DATETIME_ORIGINAL]);
    if (!taken) taken = ascii(view, ifd0[TAG_DATETIME]);

    if (!ifd0[TAG_GPS_IFD]) return null;
    var gps = readIfd(view, tiff, view.getUint32(ifd0[TAG_GPS_IFD].at, little), little);

    var lat = degrees(view, gps[GPS.LAT], ascii(view, gps[GPS.LAT_REF]), little);
    var lng = degrees(view, gps[GPS.LNG], ascii(view, gps[GPS.LNG_REF]), little);
    if (lat === null || lng === null) return null;
    // A camera with GPS on but no fix writes zeros rather than omitting the
    // tags, which would otherwise plant the flight in the Gulf of Guinea.
    if (Math.abs(lat) < 1e-9 && Math.abs(lng) < 1e-9) return null;
    if (!(lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180)) return null;

    var alt = null;
    if (gps[GPS.ALT]) {
      alt = rational(view, gps[GPS.ALT].at, little);
      if (gps[GPS.ALT_REF] && view.getUint8(gps[GPS.ALT_REF].at) === 1) alt = -alt;
    }

    return { lat: lat, lng: lng, taken: taken || null, alt: alt };
  }

  function readSlice(file, bytes) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { reject(reader.error); };
      reader.readAsArrayBuffer(file.slice(0, bytes));
    });
  }

  /* Read one file. Resolves to a fix, or null if it has no usable GPS. */
  function readFile(file) {
    return readSlice(file, HEAD_BYTES)
      .then(function (buf) {
        try {
          return parse(buf);
        } catch (err) {
          if (!(err instanceof OutOfRange)) return null;
          // An offset pointed past the first slice — unusual, but a file with a
          // large maker note can do it. Try once with more.
          return readSlice(file, RETRY_BYTES).then(function (bigger) {
            try { return parse(bigger); } catch (e) { return null; }
          });
        }
      })
      .catch(function () { return null; });
  }

  /* Read many, in order, reporting progress. Sequential on purpose: hundreds of
     concurrent FileReaders on a phone is how you get an out-of-memory tab. */
  function readFiles(files, onProgress) {
    var fixes = [], skipped = [], index = 0;
    function step() {
      if (index >= files.length) return Promise.resolve({ fixes: fixes, skipped: skipped });
      var file = files[index++];
      if (onProgress) onProgress(index, files.length);
      return readFile(file).then(function (fix) {
        if (fix) fixes.push(fix);
        else skipped.push(file.name || "photo");
        return step();
      });
    }
    return step();
  }

  return { readFile: readFile, readFiles: readFiles };
})();
