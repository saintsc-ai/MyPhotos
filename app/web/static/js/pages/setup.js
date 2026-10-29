// Page script for setup.html — moved verbatim out of an inline
// <script> so the Content-Security-Policy can forbid inline script.
    // i18n bootstrap. Like login.html, ignore the admin's default
    // language (no admin to read it from on first run) — let
    // localStorage["myphotos-lang"] win, otherwise default to ko
    // since the seed UI strings were written there.
    if (window.i18n) {
      i18n.init("ko").then(() => {
        document.documentElement.lang = i18n.getCurrentLang();
      });
    }

    // ---- Simple state machine -----------------------------------------
    let step = 1;
    const dots = document.querySelectorAll(".progress .dot");
    const sections = document.querySelectorAll(".step");

    function showStep(n) {
      step = n;
      sections.forEach(s => s.classList.toggle("active", Number(s.dataset.step) === n));
      dots.forEach(d => {
        const i = Number(d.dataset.stepDot);
        d.classList.toggle("done", i < n);
        d.classList.toggle("active", i === n);
      });
    }

    function setMsg(id, text, kind) {
      const el = document.getElementById(id);
      el.textContent = text || "";
      el.className = "msg" + (kind ? " " + kind : "");
    }

    // ---- Bail out if setup is already done ---------------------------
    fetch("/api/setup/status").then(r => r.ok ? r.json() : null).then(s => {
      if (s && !s.needs_admin) {
        location.replace("/");
      }
    }).catch(() => { /* let the user try anyway */ });

    // ---- Step 1: admin password --------------------------------------
    const step1Btn = document.getElementById("step1-submit");
    step1Btn.addEventListener("click", async () => {
      const display_name = document.getElementById("display_name").value.trim();
      const password  = document.getElementById("password").value;
      const password2 = document.getElementById("password2").value;
      if (password.length < 8) {
        setMsg("step1-msg",
          (window._t || ((k,f)=>f))("setup.step1.err_short", "비밀번호는 최소 8자 이상이어야 합니다."), "err");
        return;
      }
      if (password !== password2) {
        setMsg("step1-msg",
          (window._t || ((k,f)=>f))("setup.step1.err_mismatch", "비밀번호가 일치하지 않습니다."), "err");
        return;
      }
      setMsg("step1-msg", (window._t || ((k,f)=>f))("setup.saving", "저장 중…"), "");
      step1Btn.disabled = true;
      try {
        const res = await fetch("/api/setup/admin", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({
            password,
            display_name: display_name || null,
          }),
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch (_) {}
          setMsg("step1-msg",
            (window._tn || ((k,f,p)=>f))("setup.step1.err_save", "저장 실패: {detail}", { detail }), "err");
          step1Btn.disabled = false;
          return;
        }
        setMsg("step1-msg", "");
        showStep(2);
      } catch (e) {
        setMsg("step1-msg",
          (window._t || ((k,f)=>f))("common.network_error", "네트워크 오류") + ": " + e.message, "err");
        step1Btn.disabled = false;
      }
    });

    // ---- Step 2: photo root ------------------------------------------
    const step2Btn = document.getElementById("step2-submit");
    document.getElementById("step2-skip").addEventListener("click", () => {
      showStep(3);
      refreshMlOnce();
    });
    step2Btn.addEventListener("click", async () => {
      const label    = document.getElementById("root_label").value.trim();
      const abs_path = document.getElementById("root_abs_path").value.trim();
      const readonly = document.getElementById("root_readonly").checked;
      const _t  = window._t  || ((k,f)=>f);
      const _tn = window._tn || ((k,f,p)=>f);
      if (!label)    { setMsg("step2-msg", _t("setup.step2.err_label", "라벨을 입력하세요."), "err"); return; }
      if (!abs_path) { setMsg("step2-msg", _t("setup.step2.err_path", "절대 경로를 입력하세요."), "err"); return; }
      setMsg("step2-msg", _t("setup.step2.adding", "추가 중…"), "");
      step2Btn.disabled = true;
      try {
        const res = await fetch("/api/admin/roots", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ label, abs_path, readonly, enabled: true }),
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try { detail = (await res.json()).detail || detail; } catch (_) {}
          setMsg("step2-msg", _tn("setup.step2.err_add", "추가 실패: {detail}", { detail }), "err");
          step2Btn.disabled = false;
          return;
        }
        document.getElementById("step4-msg").textContent =
          _tn("setup.step4.sub_with_root",
            "사진 폴더 \"{label}\"이 등록되었습니다. 관리 UI에서 스캔을 시작하면 인덱싱이 진행됩니다.",
            { label });
        showStep(3);
        refreshMlOnce();
      } catch (e) {
        setMsg("step2-msg",
          _t("common.network_error", "네트워크 오류") + ": " + e.message, "err");
        step2Btn.disabled = false;
      }
    });

    // ---- Step 3: ML model download ------------------------------------
    const step3Skip  = document.getElementById("step3-skip");
    const step3Start = document.getElementById("step3-start");
    const mlFilesEl  = document.getElementById("ml-files");
    const mlLogEl    = document.getElementById("ml-log");
    let mlPoll = null;

    function fmtBytes(n) {
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
      return (n / 1024 / 1024).toFixed(1) + " MB";
    }

    function renderMl(status) {
      const _t  = window._t  || ((k,f)=>f);
      const _tn = window._tn || ((k,f,p)=>f);
      const rows = (status.files || []).map(f => {
        const icon = f.done ? "✅" : (status.running ? "⏳" : "⬜");
        const sz = f.size ? fmtBytes(f.size) : "—";
        const need = fmtBytes(f.min_bytes);
        return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;">
          <span style="width:22px;text-align:center">${icon}</span>
          <span style="flex:1;color:${f.done ? '#9eebb8' : '#bbb'};">${f.name}</span>
          <span style="color:#777;font-variant-numeric:tabular-nums;">${sz} / ${need}</span>
        </div>`;
      }).join("");
      mlFilesEl.innerHTML = rows;
      mlLogEl.textContent = (status.log_tail || []).join("\n");
      mlLogEl.scrollTop = mlLogEl.scrollHeight;

      if (status.running) {
        step3Start.disabled = true;
        step3Start.textContent = _t("setup.step3.downloading", "다운로드 중…");
        setMsg("step3-msg", "", "");
      } else if (status.all_done) {
        step3Start.disabled = true;
        step3Start.textContent = _t("setup.step3.done", "완료");
        step3Skip.textContent = _t("setup.next", "다음");
        setMsg("step3-msg", _t("setup.step3.all_ready", "모든 모델이 준비되었습니다."), "ok");
      } else if (status.return_code != null && status.return_code !== 0) {
        step3Start.disabled = false;
        step3Start.textContent = _t("setup.step3.retry", "다시 시도");
        setMsg("step3-msg",
          _tn("setup.step3.err_failed",
            "다운로드 실패 (exit {rc}). 위 로그 확인.",
            { rc: status.return_code }), "err");
      } else {
        step3Start.disabled = false;
        step3Start.textContent = _t("setup.step3.start", "다운로드 시작");
      }
    }

    async function refreshMlOnce() {
      try {
        const r = await fetch("/api/setup/ml-models/status", { credentials: "same-origin" });
        if (!r.ok) return;
        const s = await r.json();
        renderMl(s);
        if (s.running) startMlPoll();
        else stopMlPoll();
      } catch (_) {}
    }
    function startMlPoll() {
      if (mlPoll) return;
      mlPoll = setInterval(refreshMlOnce, 2000);
    }
    function stopMlPoll() {
      if (!mlPoll) return;
      clearInterval(mlPoll); mlPoll = null;
    }

    step3Skip.addEventListener("click", () => { stopMlPoll(); showStep(4); });
    step3Start.addEventListener("click", async () => {
      const _t  = window._t  || ((k,f)=>f);
      step3Start.disabled = true;
      setMsg("step3-msg", _t("setup.step3.starting", "시작하는 중…"), "");
      try {
        const r = await fetch("/api/setup/ml-models", {
          method: "POST",
          credentials: "same-origin",
        });
        if (!r.ok) {
          let detail = `HTTP ${r.status}`;
          try { detail = (await r.json()).detail || detail; } catch (_) {}
          setMsg("step3-msg",
            (window._tn || ((k,f,p)=>f))("setup.step3.err_start", "시작 실패: {detail}", { detail }), "err");
          step3Start.disabled = false;
          return;
        }
        renderMl(await r.json());
        startMlPoll();
      } catch (e) {
        setMsg("step3-msg",
          _t("common.network_error", "네트워크 오류") + ": " + e.message, "err");
        step3Start.disabled = false;
      }
    });

    // ---- Step 4: redirect --------------------------------------------
    document.getElementById("goto-admin").addEventListener("click", () => {
      location.replace("/admin.html");
    });
    document.getElementById("goto-gallery").addEventListener("click", () => {
      location.replace("/");
    });
