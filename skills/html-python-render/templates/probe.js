// The page half of the contract. Drop into index.html and adapt.
//
// Three states, three behaviours, and the page is useful in all of them. The
// input is never disabled: locking a text field while a probe is pending means
// someone who starts the backend afterwards is stuck until they reload, and
// cannot even type while waiting.

var SERVICE = "my-app";
var state = { backendOk: false };

function probeBackend(){
  var settled = false;
  function done(ok){
    if (settled) return;
    settled = true;
    state.backendOk = ok;
    renderHint();
  }
  try {
    fetch("api/health", { cache: "no-store" })
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(j){ done(!!(j && j.service === SERVICE)); })
      .catch(function(){ done(false); });
  } catch (e){ done(false); }
  setTimeout(function(){ done(false); }, 4000);   // never hang on a dead host
}

function renderHint(){
  // Say what the button will do, not what the browser forbids. A control that
  // is visible but useless, with a paragraph of explanation under it, reads as
  // an error on a page where nothing has gone wrong.
  document.getElementById("hint").textContent = state.backendOk
    ? "Fetched live."
    : "Start the helper to fetch live, or open one of the prepared results.";
}

function run(params){
  var btn = document.getElementById("go");
  btn.disabled = true;
  setProgress(1, "starting");

  function fail(msg){
    setProgress(0, "");
    document.getElementById("status").textContent = msg;
    btn.disabled = false;
    probeBackend();                 // it may have gone away; catch up
  }

  function poll(id){
    fetch("api/progress?id=" + encodeURIComponent(id), { cache: "no-store" })
      .then(function(r){ return r.json(); })
      .then(function(j){
        setProgress(j.pct, j.stage);
        if (!j.done) return setTimeout(function(){ poll(id); }, 300);
        if (j.error) return fail(j.error);
        show(j.payload);
        btn.disabled = false;
        revealResult();
      })
      .catch(function(){ fail("The helper stopped answering."); });
  }

  fetch("api/start?" + new URLSearchParams(params), { cache: "no-store" })
    .then(function(r){ return r.text().then(function(t){ return { ok: r.ok, text: t }; }); })
    .then(function(res){
      var j = null;
      try { j = JSON.parse(res.text); } catch (e){ /* a 404 page: not our backend */ }
      if (!j) return fail("No helper answered.");
      if (!res.ok || j.error) return fail(j.error || ("HTTP " + res.status));
      poll(j.id);
    })
    .catch(function(){ fail("No helper answered."); });
}

function revealResult(){
  // scrollIntoView has been seen doing nothing at all on a deployed page --
  // scrollY stayed at 0 while scrollTo moved fine. Compute the position, leave
  // room for a sticky header, and check once in case smooth was ignored.
  var el = document.getElementById("result");
  var r = el.getBoundingClientRect();
  if (r.top >= 0 && r.bottom <= window.innerHeight) return;
  var header = document.querySelector("header");
  var pad = (header ? header.getBoundingClientRect().height : 0) + 10;
  var target = Math.max(0, window.scrollY + r.top - pad);
  try { window.scrollTo({ top: target, behavior: "smooth" }); }
  catch (e){ window.scrollTo(0, target); }
  setTimeout(function(){
    if (Math.abs(window.scrollY - target) > 40) window.scrollTo(0, target);
  }, 400);
}
