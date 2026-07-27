(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function escapeHtml(value) {
    var element = document.createElement("div");
    element.textContent = value || "";
    return element.innerHTML;
  }

  window.showToast = function (message, type) {
    var container = document.querySelector(".toast-container");
    if (!container) {
      container = document.createElement("div");
      container.className = "toast-container";
      document.body.appendChild(container);
    }
    var toast = document.createElement("div");
    toast.className = "toast toast-" + (type || "info");
    toast.innerHTML = "<span>" + escapeHtml(message) + '</span><button type="button" class="toast__close" aria-label="Dismiss">×</button>';
    container.appendChild(toast);
    toast.querySelector(".toast__close").addEventListener("click", function () { toast.remove(); });
    setTimeout(function () { toast.remove(); }, 5000);
  };

  function initMobileNav() {
    var toggle = document.querySelector("[data-mobile-toggle]");
    var panel = document.querySelector("[data-mobile-panel]");
    if (!toggle || !panel) return;
    toggle.addEventListener("click", function () {
      panel.classList.toggle("open");
      toggle.setAttribute("aria-expanded", panel.classList.contains("open") ? "true" : "false");
    });
    panel.querySelectorAll("a").forEach(function (link) { link.addEventListener("click", function () { panel.classList.remove("open"); }); });
  }

  function initToasts() {
    document.querySelectorAll(".toast").forEach(function (toast) {
      var close = toast.querySelector(".toast__close");
      if (close) close.addEventListener("click", function () { toast.remove(); });
      setTimeout(function () { toast.remove(); }, 6000);
    });
  }

  function initConfirmModals() {
    var backdrop = document.querySelector("[data-confirm-modal]");
    if (!backdrop) return;
    var title = backdrop.querySelector("[data-confirm-title]");
    var body = backdrop.querySelector("[data-confirm-body]");
    var accept = backdrop.querySelector("[data-confirm-accept]");
    var pending = null;
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.dataset.confirmed === "true") return;
        event.preventDefault(); pending = form;
        title.textContent = form.dataset.confirmTitle || "Are you sure?";
        body.textContent = form.dataset.confirm || "This action cannot be undone.";
        backdrop.classList.add("open");
      });
    });
    function close() { backdrop.classList.remove("open"); pending = null; }
    backdrop.querySelectorAll("[data-confirm-cancel]").forEach(function (button) { button.addEventListener("click", close); });
    backdrop.addEventListener("click", function (event) { if (event.target === backdrop) close(); });
    accept.addEventListener("click", function () { if (pending) { pending.dataset.confirmed = "true"; pending.submit(); } close(); });
  }

  function initFileDrops() {
    document.querySelectorAll(".form-file-drop").forEach(function (zone) {
      var input = zone.querySelector('input[type="file"]');
      var output = zone.querySelector("[data-filename]");
      if (!input || !output) return;
      function showName() { if (input.files && input.files[0]) { output.textContent = "Selected: " + input.files[0].name; output.style.display = "block"; } }
      zone.addEventListener("click", function (event) { if (event.target.tagName !== "INPUT") input.click(); });
      input.addEventListener("change", showName);
      ["dragenter", "dragover"].forEach(function (name) { zone.addEventListener(name, function (event) { event.preventDefault(); zone.classList.add("is-dragover"); }); });
      ["dragleave", "drop"].forEach(function (name) { zone.addEventListener(name, function (event) { event.preventDefault(); zone.classList.remove("is-dragover"); }); });
      zone.addEventListener("drop", function (event) { if (event.dataTransfer.files.length) { input.files = event.dataTransfer.files; showName(); } });
    });
  }

  function initTheme() {
    var body = document.body;
    if (!body) return;
    var stored = null;
    try { stored = window.localStorage.getItem("smart-dit-theme"); } catch (error) { stored = null; }
    var preferredDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    function apply(theme, persist) {
      body.dataset.theme = theme;
      var dark = theme === "dark";
      document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
        button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
        button.setAttribute("title", dark ? "Switch to light mode" : "Switch to dark mode");
      });
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", dark ? "#082a28" : "#08766e");
      if (persist) { try { window.localStorage.setItem("smart-dit-theme", theme); } catch (error) {} }
    }
    apply(stored === "dark" || (!stored && preferredDark) ? "dark" : "light", false);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () { apply(body.dataset.theme === "dark" ? "light" : "dark", true); });
    });
  }

  function initQuickSearch() {
    var root = document.querySelector("[data-quick-search]");
    if (!root) return;
    var input = root.querySelector("[data-quick-search-input]");
    function open() {
      root.hidden = false;
      document.body.classList.add("has-overlay");
      window.setTimeout(function () { if (input) input.focus(); }, 0);
    }
    function close() {
      root.hidden = true;
      document.body.classList.remove("has-overlay");
    }
    document.querySelectorAll("[data-open-search]").forEach(function (button) {
      button.addEventListener("click", open);
    });
    root.querySelectorAll("[data-close-search]").forEach(function (button) {
      button.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); open();
      } else if (event.key === "Escape" && !root.hidden) {
        close();
      }
    });
  }

  function copyText(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(value);
    var temporary = document.createElement("textarea");
    temporary.value = value; document.body.appendChild(temporary); temporary.select();
    document.execCommand("copy"); temporary.remove(); return Promise.resolve();
  }

  function initAIAssistant() {
    var root = document.querySelector("[data-ai-root]");
    if (!root) return;
    var chat = root.querySelector("[data-chat-window]");
    var form = root.querySelector("[data-chat-form]");
    var input = root.querySelector("[data-chat-input]");
    var send = root.querySelector("[data-chat-send]");
    var moduleSelect = root.querySelector("[data-module-select]");
    var styleSelect = root.querySelector("[data-response-style]");
    var followups = root.querySelector("[data-followups]");
    var followupList = root.querySelector("[data-followup-list]");
    var title = root.querySelector("[data-chat-title]");
    var state = {
      conversationId: root.dataset.conversationId ? Number(root.dataset.conversationId) : null,
      moduleId: moduleSelect ? moduleSelect.value || null : null,
      resourceId: root.dataset.resourceId ? Number(root.dataset.resourceId) : null,
      mode: root.dataset.selectedMode || "explain",
      responseStyle: styleSelect ? styleSelect.value || "guided" : "guided",
      generating: false
    };

    function scrollToBottom() { if (chat) chat.scrollTop = chat.scrollHeight; }
    function resizeInput() { if (input) { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; } }
    function hideEmpty() { var empty = chat && chat.querySelector("[data-ai-empty-state]"); if (empty) empty.remove(); }
    function typingIndicator() {
      var element = document.createElement("div");
      element.className = "chat-turn chat-turn--assistant chat-turn--typing";
      element.innerHTML = '<div class="chat-avatar chat-avatar--assistant">✦</div><div class="typing-indicator"><span></span><span></span><span></span></div>';
      chat.appendChild(element); scrollToBottom(); return element;
    }
    function feedbackControls(messageId) {
      return '<div class="ai-feedback" data-ai-feedback data-message-id="' + messageId + '"><span>Was this useful?</span><button type="button" data-rating="5">Helpful</button><button type="button" data-rating="2">Needs improvement</button><button type="button" data-unclear="true">Report unclear</button></div>';
    }
    function actionControls() { return '<div class="chat-message-actions"><button type="button" data-copy-answer>Copy</button><button type="button" data-regenerate>↻ Regenerate</button></div>'; }
    function decorateCodeBlocks(scope) {
      scope.querySelectorAll("pre").forEach(function (pre) {
        if (pre.querySelector("[data-copy-code]")) return;
        var button = document.createElement("button");
        button.type = "button"; button.className = "code-copy"; button.dataset.copyCode = ""; button.textContent = "Copy code";
        pre.appendChild(button);
      });
    }
    function createUserTurn(message) {
      var turn = document.createElement("div"); turn.className = "chat-turn chat-turn--user";
      turn.innerHTML = '<div class="chat-avatar chat-avatar--user">You</div><div class="chat-bubble chat-bubble--user"><p class="mb-0">' + escapeHtml(message).replace(/\n/g, "<br>") + "</p></div>";
      chat.appendChild(turn); scrollToBottom();
    }
    function createAssistantTurn(html, messageId, sources, webSources, generalGuidance) {
      var turn = document.createElement("div"); turn.className = "chat-turn chat-turn--assistant"; turn.dataset.assistantTurn = ""; turn.dataset.messageId = messageId || "";
      turn.innerHTML = '<div class="chat-avatar chat-avatar--assistant">✦</div><div class="chat-turn__body"><div class="chat-bubble chat-bubble--assistant">' + html + '</div>' + actionControls() + feedbackControls(messageId || "") + '</div>';
      appendMeta(turn.querySelector(".chat-turn__body"), sources, webSources, generalGuidance);
      chat.appendChild(turn); decorateCodeBlocks(turn); scrollToBottom(); return turn;
    }
    function appendMeta(body, sources, webSources, generalGuidance) {
      sources = Array.isArray(sources) ? sources : [];
      webSources = Array.isArray(webSources) ? webSources : [];
      if (generalGuidance) {
        var notice = document.createElement("div"); notice.className = "general-guidance-note"; notice.textContent = "No directly relevant approved resource was found. This response uses general academic guidance" + ((webSources && webSources.length) ? " with supplementary web research." : "."); body.appendChild(notice);
      }
      if (sources && sources.length) {
        var wrap = document.createElement("div"); wrap.className = "chat-sources"; wrap.innerHTML = "<span>Lecturer sources used</span>";
        sources.forEach(function (source) { var link = document.createElement("a"); link.className = "source-pill"; link.href = source.url; link.target = "_blank"; link.rel = "noopener"; link.textContent = (source.verified ? "✓ " : "") + source.title; wrap.appendChild(link); });
        body.appendChild(wrap);
      }
      if (webSources && webSources.length) {
        var webWrap = document.createElement("div"); webWrap.className = "chat-sources chat-sources--web"; webWrap.innerHTML = "<span>Supplementary web sources</span>";
        webSources.forEach(function (source) { var link = document.createElement("a"); link.className = "source-pill"; link.href = source.url; link.target = "_blank"; link.rel = "noopener"; link.textContent = source.title; webWrap.appendChild(link); });
        body.appendChild(webWrap);
      }
    }
    function setFollowups(items) {
      if (!followups || !followupList) return;
      followupList.innerHTML = "";
      (items || []).slice(0, 3).forEach(function (item) { var button = document.createElement("button"); button.type = "button"; button.dataset.followup = item; button.textContent = item; followupList.appendChild(button); });
      followups.hidden = !followupList.children.length;
    }
    function addConversation(id, name) {
      var list = root.querySelector("[data-conversation-list]"); if (!list) return;
      var empty = root.querySelector("[data-conversation-empty]"); if (empty) empty.remove();
      var row = document.createElement("div"); row.className = "conv-row"; row.dataset.conversationRow = ""; row.dataset.conversationTitle = name.toLowerCase();
      row.innerHTML = '<a class="conv-item active" href="' + root.dataset.baseUrl + '?conversation_id=' + id + '"><span class="conv-item__title"></span></a><button type="button" class="conv-item__rename" data-rename-conversation="' + id + '" data-title="' + escapeHtml(name) + '" aria-label="Rename conversation">✎</button>';
      row.querySelector(".conv-item__title").textContent = name; list.insertBefore(row, list.firstChild);
      list.querySelectorAll(".conv-item").forEach(function (item, index) { if (index) item.classList.remove("active"); });
    }
    function setGenerating(value) { state.generating = value; if (send) send.disabled = value; }
    function sendMessage(message) {
      if (!message || state.generating) return;
      hideEmpty(); setFollowups([]); createUserTurn(message); setGenerating(true);
      if (input) { input.value = ""; resizeInput(); }
      var typing = typingIndicator();
      fetch(root.dataset.askUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() }, body: JSON.stringify({ message: message, mode: state.mode, module_id: state.moduleId || null, resource_id: state.resourceId || null, conversation_id: state.conversationId, response_style: state.responseStyle }) })
        .then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); })
        .then(function (result) {
          typing.remove();
          if (!result.ok || !result.data.ok) { createAssistantTurn("<p>" + escapeHtml(result.data.error || "I could not complete that response.") + "</p>", "", [], [], false); return; }
          var wasNew = !state.conversationId; state.conversationId = result.data.conversation_id;
          createAssistantTurn(result.data.answer_html, result.data.message_id, result.data.sources, result.data.web_sources, result.data.general_guidance);
          setFollowups(result.data.followups);
          if (wasNew) { addConversation(state.conversationId, result.data.conversation_title); if (title) title.textContent = result.data.conversation_title; }
        })
        .catch(function () { typing.remove(); createAssistantTurn("<p>The learning assistant could not be reached. Please try again.</p>", "", [], [], false); })
        .finally(function () { setGenerating(false); if (input) input.focus(); });
    }
    function sendFeedback(group, rating, unclear) {
      if (!group.dataset.messageId) return;
      fetch(root.dataset.feedbackUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() }, body: JSON.stringify({ message_id: group.dataset.messageId, rating: rating, is_unclear: unclear }) })
        .then(function (response) { return response.json(); }).then(function (data) { if (data.ok) group.innerHTML = "<span>Thank you — feedback recorded.</span>"; });
    }
    function regenerate(turn) {
      if (!state.conversationId || state.generating) return;
      setGenerating(true); var typing = typingIndicator();
      fetch(root.dataset.regenerateUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() }, body: JSON.stringify({ conversation_id: state.conversationId, response_style: state.responseStyle }) })
        .then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); })
        .then(function (result) {
          typing.remove(); if (!result.ok || !result.data.ok) { window.showToast(result.data.error || "Unable to regenerate the answer.", "error"); return; }
          var body = turn.querySelector(".chat-turn__body"); body.querySelector(".chat-bubble--assistant").innerHTML = result.data.answer_html;
          turn.dataset.messageId = result.data.message_id; body.querySelector(".ai-feedback").outerHTML = feedbackControls(result.data.message_id);
          body.querySelectorAll(".chat-sources, .general-guidance-note").forEach(function (node) { node.remove(); }); appendMeta(body, result.data.sources, result.data.web_sources, result.data.general_guidance); decorateCodeBlocks(turn); setFollowups(result.data.followups); scrollToBottom();
        })
        .catch(function () { typing.remove(); window.showToast("Unable to regenerate the answer.", "error"); })
        .finally(function () { setGenerating(false); });
    }
    function renameConversation(button) {
      var id = button.dataset.renameConversation; var current = button.dataset.title || (title ? title.textContent : ""); var next = window.prompt("Rename conversation", current);
      if (!next || next.trim() === current) return;
      var endpoint = root.dataset.renameUrl.replace("/0/", "/" + id + "/");
      fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() }, body: JSON.stringify({ title: next.trim() }) })
        .then(function (response) { return response.json(); }).then(function (data) { if (!data.ok) { window.showToast(data.error || "Could not rename this conversation.", "error"); return; } if (title && String(state.conversationId) === String(id)) title.textContent = data.title; root.querySelectorAll('[data-conversation-row]').forEach(function (row) { var rename = row.querySelector('[data-rename-conversation="' + id + '"]'); if (rename) { rename.dataset.title = data.title; row.dataset.conversationTitle = data.title.toLowerCase(); var label = row.querySelector('.conv-item__title'); if (label) label.textContent = data.title; } }); });
    }

    root.addEventListener("click", function (event) {
      var suggestion = event.target.closest("[data-suggest]"); if (suggestion) { sendMessage(suggestion.dataset.suggest); return; }
      var followup = event.target.closest("[data-followup]"); if (followup) { sendMessage(followup.dataset.followup); return; }
      var feedback = event.target.closest("[data-ai-feedback] button"); if (feedback) { var group = feedback.closest("[data-ai-feedback]"); sendFeedback(group, feedback.dataset.rating ? Number(feedback.dataset.rating) : null, feedback.dataset.unclear === "true"); return; }
      var copy = event.target.closest("[data-copy-answer]"); if (copy) { var answer = copy.closest("[data-assistant-turn]").querySelector(".chat-bubble--assistant"); copyText(answer.innerText).then(function () { window.showToast("Response copied.", "success"); }); return; }
      var copyCode = event.target.closest("[data-copy-code]"); if (copyCode) { copyText(copyCode.closest("pre").innerText.replace("Copy code", "")).then(function () { copyCode.textContent = "Copied"; setTimeout(function () { copyCode.textContent = "Copy code"; }, 1400); }); return; }
      var regenerateButton = event.target.closest("[data-regenerate]"); if (regenerateButton) { regenerate(regenerateButton.closest("[data-assistant-turn]")); return; }
      var rename = event.target.closest("[data-rename-conversation]"); if (rename) { renameConversation(rename); }
    });
    root.querySelectorAll(".chat-turn--assistant").forEach(decorateCodeBlocks);
    if (form) form.addEventListener("submit", function (event) { event.preventDefault(); sendMessage((input.value || "").trim()); });
    if (input) { input.addEventListener("input", resizeInput); input.addEventListener("keydown", function (event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); } }); resizeInput(); }
    if (moduleSelect) moduleSelect.addEventListener("change", function () { state.moduleId = moduleSelect.value || null; });
    if (styleSelect) styleSelect.addEventListener("change", function () { state.responseStyle = styleSelect.value || "guided"; });
    root.querySelectorAll("[data-mode-pill]").forEach(function (pill) { pill.addEventListener("click", function () { root.querySelectorAll("[data-mode-pill]").forEach(function (item) { item.classList.remove("active"); }); pill.classList.add("active"); state.mode = pill.dataset.modePill; input.placeholder = "Ask DIT AI to " + pill.textContent.toLowerCase() + "…"; input.focus(); }); });
    var search = root.querySelector("[data-conversation-search]"); if (search) search.addEventListener("input", function () { var value = search.value.trim().toLowerCase(); root.querySelectorAll("[data-conversation-row]").forEach(function (row) { row.hidden = !!value && !row.dataset.conversationTitle.includes(value); }); });
    scrollToBottom();
  }

  document.addEventListener("DOMContentLoaded", function () { initTheme(); initMobileNav(); initToasts(); initConfirmModals(); initFileDrops(); initQuickSearch(); initAIAssistant(); });
})();
