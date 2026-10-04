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

  // JSON requests that fail clearly on flaky mobile networks: a timeout
  // instead of an endless spinner, and a specific message when the session
  // expired (the server answers with a login page instead of JSON).
  function requestJSON(url, options) {
    options = options || {};
    var controller = window.AbortController ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, options.timeout || 45000) : null;
    var headers = { "X-CSRFToken": csrfToken() };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(url, {
      method: options.method || "POST",
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      var type = response.headers.get("Content-Type") || "";
      if (response.redirected || type.indexOf("application/json") === -1) {
        var expired = new Error("Your session has expired. Refresh the page and sign in again.");
        expired.kind = "session";
        throw expired;
      }
      return response.json().then(function (data) { return { ok: response.ok && data.ok !== false, status: response.status, data: data }; });
    }).catch(function (error) {
      if (error.kind === "session") throw error;
      var failure = new Error(navigator.onLine === false
        ? "You're offline. Reconnect and try again."
        : error.name === "AbortError" ? "The server took too long to respond. Please try again." : "The connection failed. Please try again.");
      failure.kind = "network";
      throw failure;
    }).finally(function () { if (timer) window.clearTimeout(timer); });
  }

  // Stops double-taps from posting a form twice, and blocks submits while
  // offline instead of leaving the user on the browser's error page.
  function initSubmitGuard() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (event.defaultPrevented || form.method.toLowerCase() !== "post") return;
      if (navigator.onLine === false) {
        event.preventDefault();
        window.showToast("You're offline. Your changes were not sent; reconnect and try again.", "warning");
        return;
      }
      if (form.dataset.submitting === "true") { event.preventDefault(); return; }
      form.dataset.submitting = "true";
      // Disable after the browser has captured the clicked button's name/value.
      window.setTimeout(function () {
        form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
        });
      }, 0);
    });
    // Pages restored from the back/forward cache must be usable again.
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      document.querySelectorAll("form[data-submitting]").forEach(function (form) {
        delete form.dataset.submitting;
        form.querySelectorAll("button[aria-busy]").forEach(function (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        });
      });
    });
  }

  function initConnectivity() {
    var banner = null;
    function show() {
      if (banner) return;
      banner = document.createElement("div");
      banner.className = "offline-banner";
      banner.setAttribute("role", "status");
      banner.textContent = "You're offline. You can keep reading this page; changes will need a connection.";
      document.body.appendChild(banner);
    }
    function hide() {
      if (!banner) return;
      banner.remove();
      banner = null;
      window.showToast("You're back online.", "success");
    }
    window.addEventListener("offline", show);
    window.addEventListener("online", hide);
    if (navigator.onLine === false) show();
  }

  function initCharCounters() {
    document.querySelectorAll("[data-char-count][maxlength]").forEach(function (field) {
      var max = Number(field.getAttribute("maxlength"));
      var counter = document.createElement("small");
      counter.className = "char-count";
      counter.setAttribute("aria-live", "polite");
      field.insertAdjacentElement("afterend", counter);
      function update() {
        counter.textContent = field.value.length + " / " + max;
        counter.classList.toggle("is-near-limit", field.value.length > max * 0.9);
      }
      field.addEventListener("input", update);
      update();
    });
  }

  // Announcement composer: say how many students the chosen module reaches.
  function initReachHint() {
    var select = document.querySelector("[data-reach-select]");
    var hint = document.querySelector("[data-reach-hint]");
    if (!select || !hint) return;
    function update() {
      var option = select.selectedOptions[0];
      if (!option || !option.value) { hint.textContent = "Choose a module to see how many students it will reach."; return; }
      var reach = Number(option.dataset.reach || 0);
      hint.textContent = reach
        ? "Will reach " + reach + " enrolled student" + (reach === 1 ? "" : "s") + "."
        : "No students are enrolled yet; it will be waiting for them when they join.";
    }
    select.addEventListener("change", update);
    update();
  }

  function initServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* optional enhancement */ });
    });
  }

  function storeSetting(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* storage unavailable */ }
  }

  // Sidebar: a slide-in drawer below 1024px, a collapsible rail above it.
  function initSidebar() {
    var sidebar = document.getElementById("app-sidebar");
    if (!sidebar) return;
    var root = document.documentElement;
    var openButton = document.querySelector("[data-sidebar-open]");
    var collapseButton = sidebar.querySelector("[data-sidebar-collapse]");
    var collapseLabel = sidebar.querySelector("[data-collapse-label]");
    var desktop = window.matchMedia("(min-width: 1024px)");

    function setDrawer(open) {
      document.body.classList.toggle("sidebar-open", open);
      if (openButton) openButton.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var first = sidebar.querySelector(".sidebar__link, .brand");
        if (first) first.focus();
      }
    }

    // Collapsed links show only icons, so give them a native tooltip.
    function syncCollapsed() {
      var collapsed = desktop.matches && root.getAttribute("data-sidebar") === "collapsed";
      sidebar.querySelectorAll("[data-nav-label]").forEach(function (link) {
        if (collapsed) link.setAttribute("title", link.dataset.navLabel);
        else link.removeAttribute("title");
      });
      if (collapseButton) {
        collapseButton.setAttribute("aria-expanded", collapsed ? "false" : "true");
        collapseButton.setAttribute("title", collapsed ? "Expand sidebar" : "Collapse sidebar");
      }
      if (collapseLabel) collapseLabel.textContent = collapsed ? "Expand sidebar" : "Collapse sidebar";
    }

    if (openButton) openButton.addEventListener("click", function () { setDrawer(true); });
    document.querySelectorAll("[data-sidebar-close]").forEach(function (button) {
      button.addEventListener("click", function () { setDrawer(false); if (openButton) openButton.focus(); });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
        setDrawer(false);
        if (openButton) openButton.focus();
      }
    });
    if (collapseButton) {
      collapseButton.addEventListener("click", function () {
        var collapse = root.getAttribute("data-sidebar") !== "collapsed";
        if (collapse) root.setAttribute("data-sidebar", "collapsed");
        else root.removeAttribute("data-sidebar");
        storeSetting("smart-dit-sidebar", collapse ? "collapsed" : "expanded");
        syncCollapsed();
      });
    }
    desktop.addEventListener("change", function () { setDrawer(false); syncCollapsed(); });
    syncCollapsed();
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
      function showName() { if (input.files && input.files[0]) { output.textContent = "Selected: " + input.files[0].name; output.hidden = false; } }
      zone.addEventListener("click", function (event) { if (event.target.tagName !== "INPUT") input.click(); });
      input.addEventListener("change", showName);
      ["dragenter", "dragover"].forEach(function (name) { zone.addEventListener(name, function (event) { event.preventDefault(); zone.classList.add("is-dragover"); }); });
      ["dragleave", "drop"].forEach(function (name) { zone.addEventListener(name, function (event) { event.preventDefault(); zone.classList.remove("is-dragover"); }); });
      zone.addEventListener("drop", function (event) { if (event.dataTransfer.files.length) { input.files = event.dataTransfer.files; showName(); } });
    });
  }

  // theme-init.js has already applied the saved theme before first paint.
  function initTheme() {
    var root = document.documentElement;
    function apply(theme, persist) {
      root.setAttribute("data-theme", theme);
      var label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
      document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
      });
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", theme === "dark" ? "#0d1b19" : "#0f766e");
      if (persist) storeSetting("smart-dit-theme", theme);
    }
    apply(root.getAttribute("data-theme") === "dark" ? "dark" : "light", false);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        apply(root.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
      });
    });
  }

  function initPasswordToggles() {
    document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
      var input = document.getElementById(button.getAttribute("aria-controls"));
      if (!input) return;
      button.addEventListener("click", function () {
        var reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        button.setAttribute("aria-pressed", reveal ? "true" : "false");
        button.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
      });
    });
  }

  function initRegistrationForm() {
    var form = document.querySelector("[data-registration-form]");
    if (!form) return;
    var identity = form.querySelector("#registration_number");
    var department = form.querySelector("[data-registration-department]");
    var hint = form.querySelector("[data-registration-hint]");
    var groups = form.querySelectorAll("[data-role-fields]");
    var programme = form.querySelector("#programme_id");
    var level = form.querySelector("#nta_level_id");
    var semester = form.querySelector("#semester_id");
    var labels = {
      student: "Student",
      lecturer: "Lecturer · Head of Department approval required",
      department_head: "Head of Department · protected activation",
      admin: "Curriculum Administrator · protected activation"
    };

    function filterOptions(select, predicate) {
      if (!select) return;
      Array.from(select.options).forEach(function (option, index) {
        var visible = index === 0 || predicate(option);
        option.hidden = !visible;
        option.disabled = !visible;
      });
      if (!select.value || (select.selectedOptions[0] && select.selectedOptions[0].disabled)) {
        select.value = "";
      }
    }

    function syncSemesters() {
      if (!level || !semester) return;
      filterOptions(semester, function (option) {
        return option.dataset.levelId === level.value;
      });
    }

    function syncLevels() {
      if (!programme || !level) return;
      filterOptions(level, function (option) {
        return option.dataset.programmeId === programme.value;
      });
      syncSemesters();
    }

    function syncProgrammes() {
      if (!department || !programme) return;
      filterOptions(programme, function (option) {
        return option.dataset.departmentId === department.value;
      });
      syncLevels();
    }

    function detectedRole() {
      var value = (identity.value || "").replace(/\s/g, "");
      if (/^2403\d{4,6}$/.test(value)) return "student";
      if (/^1403\d{4,6}$/.test(value)) return "lecturer";
      if (/^5000\d{4,6}$/.test(value)) return "department_head";
      if (/^9000\d{4,6}$/.test(value)) return "admin";
      return "";
    }

    function refreshRole() {
      var role = detectedRole();
      groups.forEach(function (group) {
        var selected = group.dataset.roleFields === role;
        group.hidden = !selected;
        group.querySelectorAll("select,input,textarea").forEach(function (field) {
          field.disabled = !selected;
          field.required = selected && field.hasAttribute("data-role-required");
        });
      });
      var departmentField = form.querySelector("[data-department-field]");
      if (departmentField) departmentField.hidden = role === "admin";
      department.required = role !== "admin";
      department.disabled = role === "admin";
      if (role === "student") syncProgrammes();
      hint.textContent = role
        ? "Account type detected: " + labels[role]
        : "Enter an 8-10 digit ID: 2403 for students, 1403 for lecturers, 5000 for the Head of Department or 9000 for the Curriculum Administrator.";
      hint.classList.toggle("is-detected", Boolean(role));
    }

    identity.addEventListener("input", refreshRole);
    department.addEventListener("change", syncProgrammes);
    programme.addEventListener("change", syncLevels);
    level.addEventListener("change", syncSemesters);
    refreshRole();
  }

  // Ctrl/Cmd+K: filter the user's pages, or send the text to role search.
  function initCommandPalette() {
    var root = document.querySelector("[data-palette]");
    if (!root) return;
    var form = root.querySelector("[data-palette-form]");
    var input = root.querySelector("[data-palette-input]");
    var empty = root.querySelector("[data-palette-empty]");
    var searchRow = root.querySelector("[data-palette-search-row]");
    var queryLabel = root.querySelector("[data-palette-query]");
    var options = Array.prototype.slice.call(root.querySelectorAll("[data-palette-option]"));
    var groups = Array.prototype.slice.call(root.querySelectorAll("[data-palette-group]"));
    var visible = [];
    var selected = -1;
    var opener = null;

    function select(index) {
      options.forEach(function (option) { option.classList.remove("is-selected"); option.setAttribute("aria-selected", "false"); });
      selected = visible.length ? (index + visible.length) % visible.length : -1;
      if (selected < 0) { input.removeAttribute("aria-activedescendant"); return; }
      var option = visible[selected];
      option.classList.add("is-selected");
      option.setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }

    function filter() {
      var term = input.value.trim().toLowerCase();
      visible = [];
      options.forEach(function (option) {
        if (option.hasAttribute("data-palette-search")) return;
        var match = !term || option.dataset.keywords.indexOf(term) !== -1;
        option.parentElement.hidden = !match;
        if (match) visible.push(option);
      });
      // Hide group headings with no visible entries.
      groups.forEach(function (group) {
        var next = group.nextElementSibling;
        var any = false;
        while (next && !next.hasAttribute("data-palette-group")) {
          if (!next.hidden && !next.hasAttribute("data-palette-search-row")) any = true;
          next = next.nextElementSibling;
        }
        group.hidden = !any;
      });
      if (searchRow) {
        searchRow.hidden = !term;
        if (queryLabel) queryLabel.textContent = input.value.trim();
        if (term) visible.push(searchRow.querySelector("[data-palette-option]"));
      }
      if (empty) empty.hidden = visible.length > 0;
      select(0);
    }

    function open() {
      opener = document.activeElement;
      root.hidden = false;
      document.body.classList.add("has-overlay");
      input.value = "";
      filter();
      window.setTimeout(function () { input.focus(); }, 0);
    }
    function close() {
      root.hidden = true;
      document.body.classList.remove("has-overlay");
      if (opener && opener.focus) opener.focus();
    }
    function activate(option) {
      if (!option) return;
      if (option.hasAttribute("data-palette-search")) form.submit();
      else window.location.href = option.getAttribute("href");
    }

    document.querySelectorAll("[data-open-palette]").forEach(function (button) {
      button.addEventListener("click", open);
    });
    root.querySelectorAll("[data-palette-close]").forEach(function (button) {
      button.addEventListener("click", close);
    });
    input.addEventListener("input", filter);
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { event.preventDefault(); select(selected + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); select(selected - 1); }
      else if (event.key === "Enter") { event.preventDefault(); activate(visible[selected]); }
    });
    root.addEventListener("mousemove", function (event) {
      var option = event.target.closest("[data-palette-option]");
      var index = visible.indexOf(option);
      if (option && index !== -1 && index !== selected) select(index);
    });
    root.addEventListener("click", function (event) {
      var option = event.target.closest("[data-palette-search]");
      if (option) activate(option);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      activate(visible[selected]);
    });
    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (root.hidden) open(); else close();
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
    var attachInput = root.querySelector("[data-chat-attach]");
    var attachChip = root.querySelector("[data-chat-attachment]");
    var state = {
      conversationId: root.dataset.conversationId ? Number(root.dataset.conversationId) : null,
      moduleId: moduleSelect ? moduleSelect.value || null : null,
      resourceId: root.dataset.resourceId ? Number(root.dataset.resourceId) : null,
      mode: root.dataset.selectedMode || "explain",
      responseStyle: styleSelect ? styleSelect.value || "guided" : "guided",
      attachment: null,
      generating: false
    };
    function clearAttachment() {
      state.attachment = null;
      if (attachInput) attachInput.value = "";
      if (attachChip) attachChip.hidden = true;
    }
    function chooseAttachment(file) {
      if (!file) return;
      if (["image/png", "image/jpeg", "image/webp", "application/pdf"].indexOf(file.type) === -1) { window.showToast("Attach a PNG, JPEG or WebP image, or a PDF.", "warning"); clearAttachment(); return; }
      if (file.size > 8 * 1024 * 1024) { window.showToast("Attachments must be 8 MB or smaller.", "warning"); clearAttachment(); return; }
      var reader = new FileReader();
      reader.onload = function () {
        state.attachment = { name: file.name, mime: file.type, data: String(reader.result).split(",")[1] || "" };
        attachChip.querySelector("[data-chat-attachment-name]").textContent = file.name;
        attachChip.hidden = false;
      };
      reader.onerror = function () { window.showToast("That file could not be read.", "error"); clearAttachment(); };
      reader.readAsDataURL(file);
    }

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
      return '<div class="ai-feedback" data-ai-feedback data-message-id="' + messageId + '">' +
        '<div class="ai-feedback__prompt"><strong>Rate this answer</strong><span>Your feedback helps DIT AI improve.</span></div>' +
        '<div class="ai-feedback__actions"><button type="button" data-rating="5">Helpful</button>' +
        '<button type="button" data-rating="3">Partly helpful</button>' +
        '<button type="button" data-report-toggle>Report incorrect answer</button></div>' +
        '<div class="ai-feedback__report" data-feedback-report hidden><label>What should be corrected?</label>' +
        '<textarea class="form-textarea" data-feedback-note rows="2" maxlength="1000" placeholder="Describe the incorrect fact, calculation, citation or missing explanation."></textarea>' +
        '<div><button type="button" data-report-submit>Send report</button><button type="button" data-report-cancel>Cancel</button></div></div>' +
        '<span class="ai-feedback__status" data-feedback-status aria-live="polite"></span></div>';
    }
    function actionControls() { return '<div class="chat-message-actions"><button type="button" data-copy-answer>Copy</button><button type="button" data-regenerate>↻ Regenerate</button></div>'; }
    function decorateCodeBlocks(scope) {
      scope.querySelectorAll("pre").forEach(function (pre) {
        if (pre.querySelector("[data-copy-code]")) return;
        var code = pre.querySelector("code");
        var language = "Code";
        if (code) {
          Array.from(code.classList).some(function (name) {
            if (name.indexOf("language-") === 0) {
              language = name.replace("language-", "").toUpperCase();
              return true;
            }
            return false;
          });
        }
        var label = document.createElement("span");
        label.className = "code-language";
        label.textContent = language;
        pre.appendChild(label);
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
    function createAssistantTurn(html, messageId, sources, webSources, generalGuidance, context) {
      var turn = document.createElement("div"); turn.className = "chat-turn chat-turn--assistant"; turn.dataset.assistantTurn = ""; turn.dataset.messageId = messageId || "";
      turn.innerHTML = '<div class="chat-avatar chat-avatar--assistant">✦</div><div class="chat-turn__body"><div class="chat-bubble chat-bubble--assistant">' + html + '</div>' + actionControls() + feedbackControls(messageId || "") + '</div>';
      appendMeta(turn.querySelector(".chat-turn__body"), sources, webSources, generalGuidance, context);
      chat.appendChild(turn); decorateCodeBlocks(turn); scrollToBottom(); return turn;
    }
    function createSourceCard(source, isWeb) {
      var link = document.createElement("a");
      link.className = "source-pill" + (isWeb ? " source-pill--web" : "");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener";
      var icon = document.createElement("span");
      icon.className = "source-pill__icon";
      icon.textContent = isWeb ? "↗" : "✓";
      var copy = document.createElement("span");
      copy.className = "source-pill__copy";
      var title = document.createElement("strong");
      title.textContent = source.title;
      var type = document.createElement("small");
      type.textContent = isWeb ? "Supplementary reference" : (source.type || "Verified lecturer material");
      copy.appendChild(title);
      copy.appendChild(type);
      link.appendChild(icon);
      link.appendChild(copy);
      return link;
    }
    function appendMeta(body, sources, webSources, generalGuidance, context) {
      sources = Array.isArray(sources) ? sources : [];
      webSources = Array.isArray(webSources) ? webSources : [];
      var label = context && context.label;
      if (label && context.text) {
        var badge = document.createElement("div");
        badge.className = "context-label context-label--" + label.replace(/[^a-z_]/g, "");
        badge.dataset.contextLabel = "";
        badge.textContent = context.text;
        body.appendChild(badge);
      }
      if (generalGuidance && label !== "outside_curriculum" && label !== "dit_curriculum") {
        var notice = document.createElement("div"); notice.className = "general-guidance-note";
        notice.innerHTML = "<strong>General guidance</strong><span>No directly relevant approved lecturer resource was found for this question. This answer uses general academic knowledge" + ((webSources && webSources.length) ? " supported by supplementary web research" : "") + "; confirm critical course details with your lecturer.</span>";
        body.appendChild(notice);
      }
      var prospectus = (context && Array.isArray(context.prospectus)) ? context.prospectus : [];
      if (prospectus.length) {
        var pWrap = document.createElement("div"); pWrap.className = "chat-sources chat-sources--prospectus"; pWrap.innerHTML = "<span>From the DIT prospectus</span>";
        prospectus.forEach(function (source) { var chip = document.createElement("span"); chip.className = "prospectus-chip"; chip.textContent = source.title; pWrap.appendChild(chip); });
        body.appendChild(pWrap);
      }
      if (sources && sources.length) {
        var wrap = document.createElement("div"); wrap.className = "chat-sources"; wrap.innerHTML = "<span>Verified lecturer resources used</span>";
        sources.forEach(function (source) { wrap.appendChild(createSourceCard(source, false)); });
        body.appendChild(wrap);
      }
      if (webSources && webSources.length) {
        var webWrap = document.createElement("div"); webWrap.className = "chat-sources chat-sources--web"; webWrap.innerHTML = "<span>Supplementary web sources</span>";
        webSources.forEach(function (source) { webWrap.appendChild(createSourceCard(source, true)); });
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
      var attachment = state.attachment;
      if ((!message && !attachment) || state.generating) return;
      hideEmpty(); setFollowups([]); createUserTurn((message || "Explain this attachment.") + (attachment ? "\n[Attached: " + attachment.name + "]" : "")); setGenerating(true);
      if (input) { input.value = ""; resizeInput(); }
      clearAttachment();
      var typing = typingIndicator();
      // Generated answers can take a while; allow two minutes before giving up.
      requestJSON(root.dataset.askUrl, { timeout: 120000, body: { message: message, attachment: attachment, mode: state.mode, module_id: state.moduleId || null, resource_id: state.resourceId || null, conversation_id: state.conversationId, response_style: state.responseStyle } })
        .then(function (result) {
          typing.remove();
          if (!result.ok) { createAssistantTurn("<p>" + escapeHtml(result.data.error || "I could not complete that response.") + "</p>", "", [], [], false); return; }
          var wasNew = !state.conversationId; state.conversationId = result.data.conversation_id;
          createAssistantTurn(result.data.answer_html, result.data.message_id, result.data.sources, result.data.web_sources, result.data.general_guidance, { label: result.data.context_label, text: result.data.context_label_text, prospectus: result.data.prospectus_sources });
          setFollowups(result.data.followups);
          if (wasNew) { addConversation(state.conversationId, result.data.conversation_title); if (title) title.textContent = result.data.conversation_title; }
        })
        .catch(function (error) {
          typing.remove();
          createAssistantTurn("<p>" + escapeHtml(error.message) + "</p>", "", [], [], false);
          // Give the unsent question back so it isn't lost on a dropped connection.
          if (input && !input.value) { input.value = message; resizeInput(); }
          if (attachment && !state.attachment && attachChip) { state.attachment = attachment; attachChip.querySelector("[data-chat-attachment-name]").textContent = attachment.name; attachChip.hidden = false; }
        })
        .finally(function () { setGenerating(false); if (input) input.focus(); });
    }
    function sendFeedback(group, rating, unclear, note) {
      if (!group.dataset.messageId) return;
      var payload = { message_id: group.dataset.messageId };
      if (rating !== null && rating !== undefined) payload.rating = rating;
      if (unclear !== null && unclear !== undefined) payload.is_unclear = unclear;
      if (note) payload.note = note;
      var status = group.querySelector("[data-feedback-status]");
      var buttons = group.querySelectorAll("button");
      buttons.forEach(function (button) { button.disabled = true; });
      if (status) status.textContent = "Sending feedback…";
      requestJSON(root.dataset.feedbackUrl, { body: payload })
        .then(function (result) {
          if (!result.ok) throw new Error(result.data.error || "Feedback could not be saved.");
          if (rating) {
            group.querySelectorAll("[data-rating]").forEach(function (button) {
              button.classList.toggle("is-selected", Number(button.dataset.rating) === rating);
            });
          }
          if (unclear) {
            var panel = group.querySelector("[data-feedback-report]");
            var field = group.querySelector("[data-feedback-note]");
            if (panel) panel.hidden = true;
            if (field) field.value = "";
          }
          if (status) status.textContent = result.data.message || "Thank you — feedback recorded.";
        })
        .catch(function (error) {
          if (status) status.textContent = error.message;
          window.showToast(error.message, "error");
        })
        .finally(function () { buttons.forEach(function (button) { button.disabled = false; }); });
    }
    function regenerate(turn) {
      if (!state.conversationId || state.generating) return;
      setGenerating(true); var typing = typingIndicator();
      requestJSON(root.dataset.regenerateUrl, { timeout: 120000, body: { conversation_id: state.conversationId, response_style: state.responseStyle } })
        .then(function (result) {
          typing.remove(); if (!result.ok) { window.showToast(result.data.error || "Unable to regenerate the answer.", "error"); return; }
          var body = turn.querySelector(".chat-turn__body"); body.querySelector(".chat-bubble--assistant").innerHTML = result.data.answer_html;
          turn.dataset.messageId = result.data.message_id; body.querySelector(".ai-feedback").outerHTML = feedbackControls(result.data.message_id);
          body.querySelectorAll(".chat-sources, .general-guidance-note, [data-context-label]").forEach(function (node) { node.remove(); }); appendMeta(body, result.data.sources, result.data.web_sources, result.data.general_guidance, { label: result.data.context_label, text: result.data.context_label_text, prospectus: result.data.prospectus_sources }); decorateCodeBlocks(turn); setFollowups(result.data.followups); scrollToBottom();
        })
        .catch(function (error) { typing.remove(); window.showToast(error.message, "error"); })
        .finally(function () { setGenerating(false); });
    }
    function renameConversation(button) {
      var id = button.dataset.renameConversation; var current = button.dataset.title || (title ? title.textContent : ""); var next = window.prompt("Rename conversation", current);
      if (!next || next.trim() === current) return;
      var endpoint = root.dataset.renameUrl.replace("/0/", "/" + id + "/");
      requestJSON(endpoint, { body: { title: next.trim() } })
        .catch(function (error) { window.showToast(error.message, "error"); return null; })
        .then(function (result) { if (!result) return; var data = result.data; if (!result.ok) { window.showToast(data.error || "Could not rename this conversation.", "error"); return; } if (title && String(state.conversationId) === String(id)) title.textContent = data.title; root.querySelectorAll('[data-conversation-row]').forEach(function (row) { var rename = row.querySelector('[data-rename-conversation="' + id + '"]'); if (rename) { rename.dataset.title = data.title; row.dataset.conversationTitle = data.title.toLowerCase(); var label = row.querySelector('.conv-item__title'); if (label) label.textContent = data.title; } }); });
    }

    root.addEventListener("click", function (event) {
      var suggestion = event.target.closest("[data-suggest]"); if (suggestion) { sendMessage(suggestion.dataset.suggest); return; }
      var followup = event.target.closest("[data-followup]"); if (followup) { sendMessage(followup.dataset.followup); return; }
      var ratingButton = event.target.closest("[data-ai-feedback] [data-rating]"); if (ratingButton) { var ratingGroup = ratingButton.closest("[data-ai-feedback]"); sendFeedback(ratingGroup, Number(ratingButton.dataset.rating), null, ""); return; }
      var reportToggle = event.target.closest("[data-ai-feedback] [data-report-toggle]"); if (reportToggle) { var reportGroup = reportToggle.closest("[data-ai-feedback]"); var reportPanel = reportGroup.querySelector("[data-feedback-report]"); reportPanel.hidden = !reportPanel.hidden; if (!reportPanel.hidden) reportGroup.querySelector("[data-feedback-note]").focus(); return; }
      var reportCancel = event.target.closest("[data-ai-feedback] [data-report-cancel]"); if (reportCancel) { reportCancel.closest("[data-ai-feedback]").querySelector("[data-feedback-report]").hidden = true; return; }
      var reportSubmit = event.target.closest("[data-ai-feedback] [data-report-submit]"); if (reportSubmit) { var issueGroup = reportSubmit.closest("[data-ai-feedback]"); var issueNote = issueGroup.querySelector("[data-feedback-note]").value.trim(); if (issueNote.length < 6) { window.showToast("Please briefly describe what looks incorrect.", "warning"); return; } sendFeedback(issueGroup, 1, true, issueNote); return; }
      var copy = event.target.closest("[data-copy-answer]"); if (copy) { var answer = copy.closest("[data-assistant-turn]").querySelector(".chat-bubble--assistant"); var answerContent = answer.querySelector(".ai-answer-document__content") || answer; copyText(answerContent.innerText).then(function () { window.showToast("Response copied.", "success"); }); return; }
      var copyCode = event.target.closest("[data-copy-code]"); if (copyCode) { var code = copyCode.closest("pre").querySelector("code"); copyText(code ? code.textContent : copyCode.closest("pre").innerText.replace("Copy code", "")).then(function () { copyCode.textContent = "Copied"; setTimeout(function () { copyCode.textContent = "Copy code"; }, 1400); }); return; }
      var regenerateButton = event.target.closest("[data-regenerate]"); if (regenerateButton) { regenerate(regenerateButton.closest("[data-assistant-turn]")); return; }
      var rename = event.target.closest("[data-rename-conversation]"); if (rename) { renameConversation(rename); }
    });
    root.querySelectorAll(".chat-turn--assistant").forEach(decorateCodeBlocks);
    if (form) form.addEventListener("submit", function (event) { event.preventDefault(); sendMessage((input.value || "").trim()); });
    if (attachInput) {
      root.querySelector("[data-chat-attach-button]").addEventListener("click", function () { attachInput.click(); });
      attachInput.addEventListener("change", function () { chooseAttachment(attachInput.files && attachInput.files[0]); });
      root.querySelector("[data-chat-attachment-clear]").addEventListener("click", clearAttachment);
    }
    if (input) { input.addEventListener("input", resizeInput); input.addEventListener("keydown", function (event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); } }); resizeInput(); }
    if (moduleSelect) moduleSelect.addEventListener("change", function () { state.moduleId = moduleSelect.value || null; });
    if (styleSelect) styleSelect.addEventListener("change", function () { state.responseStyle = styleSelect.value || "guided"; });
    root.querySelectorAll("[data-mode-pill]").forEach(function (pill) { pill.addEventListener("click", function () { root.querySelectorAll("[data-mode-pill]").forEach(function (item) { item.classList.remove("active"); }); pill.classList.add("active"); state.mode = pill.dataset.modePill; input.placeholder = "Ask DIT AI to " + pill.textContent.toLowerCase() + "…"; input.focus(); }); });
    var search = root.querySelector("[data-conversation-search]"); if (search) search.addEventListener("input", function () { var value = search.value.trim().toLowerCase(); root.querySelectorAll("[data-conversation-row]").forEach(function (row) { row.hidden = !!value && !row.dataset.conversationTitle.includes(value); }); });
    scrollToBottom();
  }

  function filterSelectOptions(select, attribute, value) {
    if (!select) return;
    Array.from(select.options).forEach(function (option, index) {
      if (index === 0) { option.hidden = false; option.disabled = false; return; }
      var visible = String(option.dataset[attribute] || "") === String(value || "");
      option.hidden = !visible;
      option.disabled = !visible;
    });
    if (select.selectedOptions[0] && select.selectedOptions[0].disabled) select.value = "";
  }

  function initCurriculumForm() {
    var form = document.querySelector("[data-curriculum-form]");
    if (!form) return;
    var programme = form.querySelector("[data-programme]");
    var level = form.querySelector("[data-level]");
    var semester = form.querySelector("[data-semester]");
    function programmeChanged() {
      filterSelectOptions(level, "programmeId", programme.value);
      level.value = "";
      filterSelectOptions(semester, "levelId", "");
      semester.value = "";
    }
    function levelChanged() {
      filterSelectOptions(semester, "levelId", level.value);
      semester.value = "";
    }
    programme.addEventListener("change", programmeChanged);
    level.addEventListener("change", levelChanged);
    programmeChanged();
  }

  function initQuestionForm() {
    var form = document.querySelector("[data-question-form]");
    if (!form) return;
    var module = form.querySelector("[data-question-module]");
    var topic = form.querySelector("[data-question-topic]");
    var lecturer = form.querySelector("[data-question-lecturer]");
    function changed() {
      filterSelectOptions(topic, "moduleId", module.value);
      filterSelectOptions(lecturer, "moduleId", module.value);
      topic.value = "";
      lecturer.value = "";
      var availableLecturers = Array.from(lecturer.options).filter(function (option, index) {
        return index > 0 && !option.disabled;
      });
      if (availableLecturers.length === 1) lecturer.value = availableLecturers[0].value;
    }
    module.addEventListener("change", changed);
    changed();
  }

  function initFlashcards() {
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-flashcard-toggle]");
      if (!button) return;
      var card = button.closest("[data-flashcard]");
      var flipped = card.classList.toggle("is-flipped");
      button.setAttribute("aria-expanded", flipped ? "true" : "false");
    });
  }

  function initStudyTracker() {
    var root = document.querySelector("[data-study-root]");
    if (!root) return;
    var status = root.querySelector("[data-study-status]");
    var detail = root.querySelector("[data-study-detail]");
    var bar = root.querySelector("[data-study-bar]");
    var token = null;
    var qualified = false;
    var lastActivity = Date.now();
    ["pointerdown", "keydown", "scroll", "touchstart"].forEach(function (name) {
      document.addEventListener(name, function () { lastActivity = Date.now(); }, { passive: true });
    });
    function update(seconds, isQualified) {
      var percent = Math.min(100, Math.round((seconds / 600) * 100));
      if (bar) bar.style.width = percent + "%";
      if (isQualified) {
        qualified = true;
        if (status) status.textContent = "Focused study complete";
        if (detail) detail.textContent = "This 10-minute session qualifies today’s learning streak.";
        root.querySelector("[data-study-progress]").classList.add("is-qualified");
      } else {
        if (status) status.textContent = "Focused study in progress · " + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
        if (detail) detail.textContent = "Stay on this resource and interact naturally. Idle or background time is not counted.";
      }
    }
    requestJSON(root.dataset.studyStartUrl, { timeout: 20000 }).then(function (result) {
      if (!result.ok) return;
      token = result.data.token;
      update(result.data.active_seconds || 0, result.data.qualified);
    }).catch(function () {
      if (status) status.textContent = "Study timer unavailable";
      if (detail) detail.textContent = "You can continue reading; activity tracking will retry on your next resource.";
    });
    window.setInterval(function () {
      if (!token || qualified || navigator.onLine === false || document.hidden || !document.hasFocus() || Date.now() - lastActivity > 90000) return;
      var endpoint = root.dataset.studyHeartbeatTemplate.replace("STUDY_TOKEN", encodeURIComponent(token));
      // A missed heartbeat only means that interval isn't counted; retry on the next tick.
      requestJSON(endpoint, { timeout: 20000 })
        .then(function (result) { if (result.ok) update(result.data.active_seconds || 0, result.data.qualified); })
        .catch(function () {});
    }, 30000);
  }

  function initDeclarativeActions() {
    document.querySelectorAll("[data-submit-on-change]").forEach(function (control) {
      control.addEventListener("change", function () {
        if (control.form) control.form.requestSubmit();
      });
    });
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-submit-form]");
      if (!button) return;
      var form = document.getElementById(button.dataset.submitForm);
      if (form) form.requestSubmit();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initSidebar();
    initCommandPalette();
    initPasswordToggles();
    initSubmitGuard();
    initConnectivity();
    initCharCounters();
    initReachHint();
    initServiceWorker();
    initToasts();
    initConfirmModals();
    initFileDrops();
    initRegistrationForm();
    initAIAssistant();
    initCurriculumForm();
    initQuestionForm();
    initFlashcards();
    initStudyTracker();
    initDeclarativeActions();
  });
})();
