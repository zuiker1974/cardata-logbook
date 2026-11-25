// Pikamuokkaus: ajotyyppi, tarkoitus ja kommentti tallentuvat heti (muutos / Enter / kentästä poistuminen)
(function () {
  function save(el) {
    var url = el.dataset.url;
    var value = el.value;
    if (el.dataset.last === value) return;
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field: el.dataset.field, value: value }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        el.classList.remove("saved", "save-err");
        if (res.ok && res.j.ok) {
          el.dataset.last = value;
          el.classList.add("saved");
          el.title = "Tallennettu";
          setTimeout(function () { el.classList.remove("saved"); }, 1200);
        } else {
          el.classList.add("save-err");
          el.title = (res.j && res.j.error) || "Tallennus epäonnistui";
          alertBox(el.title);
        }
      })
      .catch(function () { el.classList.add("save-err"); alertBox("Tallennus epäonnistui (yhteysvirhe)"); });
  }
  function alertBox(text) {
    var box = document.getElementById("live-msg");
    if (box) { box.textContent = text; box.style.display = "block"; }
  }
  document.querySelectorAll("[data-field]").forEach(function (el) {
    el.dataset.last = el.value;
    if (el.tagName === "SELECT") {
      el.addEventListener("change", function () { save(el); });
    } else {
      el.addEventListener("blur", function () { save(el); });
      el.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); el.blur(); } });
    }
  });
  // viimeksi valittu auto muistetaan selaimessa
  var car = document.body.dataset.vehicle;
  try {
    if (car) localStorage.setItem("bmwLogbookLastVehicle", car);
    var last = localStorage.getItem("bmwLogbookLastVehicle");
    if (last) document.querySelectorAll("[data-car='" + last + "']").forEach(function (c) { c.classList.add("last"); });
  } catch (e) { /* localStorage ei käytettävissä */ }
  var merge = document.getElementById("merge-form");
  if (merge) {
    merge.addEventListener("submit", function (e) {
      var n = merge.querySelectorAll("input[name=ids]:checked").length;
      if (n < 2) { e.preventDefault(); alertBox("Valitse vähintään kaksi saman auton matkaa yhdistettäväksi."); }
    });
  }
})();
