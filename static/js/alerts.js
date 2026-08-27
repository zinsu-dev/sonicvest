(function(){
  if (window.FinTechAlertsLoaded) return;
  window.FinTechAlertsLoaded = true;

  const styles = `
  .ft-toast-container {
    position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
    z-index: 99999; display: flex; flex-direction: column; gap: 8px;
    width: min(92vw, 400px); pointer-events: none;
  }
  .ft-toast {
    pointer-events: all; display: flex; align-items: center; gap: 10px;
    padding: 12px 14px; border-radius: 10px; font-size: 0.875rem; font-weight: 500;
    background: #181A20; border: 1px solid #2C313A; color: #F8FAFC;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    opacity: 0; transform: translateY(-8px); animation: ft-in 180ms ease-out forwards;
    border-left: 3px solid #D4AF37;
  }
  .ft-toast.success { border-left-color: #22C55E; }
  .ft-toast.warning { border-left-color: #F59E0B; }
  .ft-toast.danger, .ft-toast.error { border-left-color: #EF4444; }
  .ft-toast.info { border-left-color: #3B82F6; }
  .ft-toast .ft-icon {
    flex-shrink: 0; width: 18px; height: 18px;
  }
  .ft-toast .ft-icon svg { width: 18px; height: 18px; }
  .ft-toast .ft-msg { flex: 1; line-height: 1.4; }
  .ft-toast .ft-close {
    flex-shrink: 0; background: none; border: none; color: #6B7280;
    cursor: pointer; padding: 2px; display: flex; align-items: center; justify-content: center;
  }
  .ft-toast .ft-close:hover { color: #F8FAFC; }
  .ft-toast .ft-close svg { width: 14px; height: 14px; }
  @keyframes ft-in { to { opacity: 1; transform: translateY(0); } }
  @keyframes ft-out { to { opacity: 0; transform: translateY(-6px); } }

  .ft-confirm-overlay {
    position: fixed; inset: 0; z-index: 999999; background: rgba(0,0,0,0.6);
    display: flex; align-items: center; justify-content: center;
    opacity: 0; animation: ft-in 150ms ease-out forwards;
    backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  }
  .ft-confirm-box {
    background: #181A20; border: 1px solid #2C313A; border-radius: 14px;
    padding: 24px 20px 16px; width: min(88vw, 340px); text-align: center;
    box-shadow: 0 16px 48px rgba(0,0,0,0.5);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  .ft-confirm-icon { margin-bottom: 12px; }
  .ft-confirm-icon svg { width: 32px; height: 32px; }
  .ft-confirm-msg {
    color: #F8FAFC; font-size: 0.9375rem; font-weight: 600;
    line-height: 1.4; margin-bottom: 20px;
  }
  .ft-confirm-btns { display: flex; gap: 10px; }
  .ft-confirm-btns button {
    flex: 1; padding: 10px; border-radius: 10px; border: none;
    font-size: 0.875rem; font-weight: 700; cursor: pointer;
    transition: transform 0.12s ease, opacity 0.12s ease;
  }
  .ft-confirm-btns button:active { transform: scale(0.97); }
  .ft-confirm-cancel {
    background: #2C313A; color: #F8FAFC;
  }
  .ft-confirm-cancel:hover { background: #3A3F4A; }
  .ft-confirm-ok {
    background: #D4AF37; color: #09090B;
  }
  .ft-confirm-ok:hover { background: #E5C158; }
  .ft-confirm-ok.danger { background: #EF4444; color: #fff; }
  .ft-confirm-ok.danger:hover { background: #DC2626; }
  .ft-prompt-input {
    width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #2C313A;
    background: #09090B; color: #F8FAFC; font-size: 0.875rem; font-weight: 500;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    outline: none; box-sizing: border-box;
  }
  .ft-prompt-input:focus { border-color: #D4AF37; }
  .ft-prompt-input::placeholder { color: #6B7280; }
  `;

  const s = document.createElement('style');
  s.textContent = styles;
  document.head.appendChild(s);

  function ensureContainer(){
    let c = document.querySelector('.ft-toast-container');
    if(!c){ c = document.createElement('div'); c.className='ft-toast-container'; document.body.appendChild(c); }
    return c;
  }

  function iconFor(type){
    const map = {
      success: '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#22C55E" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
      warning: '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#F59E0B" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
      danger:  '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#EF4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    };
    return map[type] || '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#D4AF37" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
  }

  function closeIcon(){
    return '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  }

  window.showToast = function(message, type, options){
    type = type || 'info';
    const timeout = (options && options.timeout) || 3500;
    const title = options && options.title;
    const container = ensureContainer();

    const el = document.createElement('div');
    el.className = 'ft-toast ' + type;
    el.setAttribute('role','status');
    el.setAttribute('aria-live','polite');
    el.innerHTML =
      '<span class="ft-icon">' + iconFor(type) + '</span>' +
      '<div class="ft-msg">' + (title ? '<strong>' + title + '</strong><br>' : '') + message + '</div>' +
      '<button class="ft-close" aria-label="Close">' + closeIcon() + '</button>';

    el.querySelector('.ft-close').addEventListener('click', dismiss);
    container.appendChild(el);

    var timer;
    if(timeout > 0) timer = setTimeout(dismiss, timeout);

    function dismiss(){
      if(timer) clearTimeout(timer);
      el.style.animation = 'ft-out 160ms ease-in forwards';
      setTimeout(function(){ el.remove(); }, 170);
    }

    return { dismiss: dismiss };
  };

  window.showAlert = function(message, type){
    window.showToast(message, type || 'info');
  };

  window.showConfirm = function(message, options){
    options = options || {};
    var confirmText = options.confirmText || 'Confirm';
    var cancelText = options.cancelText || 'Cancel';
    var danger = options.danger || false;
    var icon = options.icon || null;

    return new Promise(function(resolve){
      var overlay = document.createElement('div');
      overlay.className = 'ft-confirm-overlay';

      var iconHtml = icon || '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#D4AF37" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';

      overlay.innerHTML =
        '<div class="ft-confirm-box">' +
          '<div class="ft-confirm-icon">' + iconHtml + '</div>' +
          '<div class="ft-confirm-msg">' + message + '</div>' +
          '<div class="ft-confirm-btns">' +
            '<button class="ft-confirm-cancel">' + cancelText + '</button>' +
            '<button class="ft-confirm-ok' + (danger ? ' danger' : '') + '">' + confirmText + '</button>' +
          '</div>' +
        '</div>';

      document.body.appendChild(overlay);

      var cancelBtn = overlay.querySelector('.ft-confirm-cancel');
      var okBtn = overlay.querySelector('.ft-confirm-ok');

      function close(result){
        overlay.style.animation = 'ft-out 120ms ease-in forwards';
        setTimeout(function(){ overlay.remove(); }, 130);
        resolve(result);
      }

      cancelBtn.addEventListener('click', function(){ close(false); });
      okBtn.addEventListener('click', function(){ close(true); });
      overlay.addEventListener('click', function(e){ if(e.target === overlay) close(false); });
    });
  };

  window.showPrompt = function(message, options){
    options = options || {};
    var placeholder = options.placeholder || '';
    var defaultValue = options.defaultValue || '';
    var confirmText = options.confirmText || 'OK';
    var cancelText = options.cancelText || 'Cancel';

    return new Promise(function(resolve){
      var overlay = document.createElement('div');
      overlay.className = 'ft-confirm-overlay';

      overlay.innerHTML =
        '<div class="ft-confirm-box">' +
          '<div class="ft-confirm-icon"><svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#D4AF37" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg></div>' +
          '<div class="ft-confirm-msg">' + message + '</div>' +
          '<input type="text" class="ft-prompt-input" value="' + defaultValue.replace(/"/g, '&quot;') + '" placeholder="' + placeholder.replace(/"/g, '&quot;') + '">' +
          '<div class="ft-confirm-btns" style="margin-top:16px">' +
            '<button class="ft-confirm-cancel">' + cancelText + '</button>' +
            '<button class="ft-confirm-ok">' + confirmText + '</button>' +
          '</div>' +
        '</div>';

      document.body.appendChild(overlay);

      var input = overlay.querySelector('.ft-prompt-input');
      var cancelBtn = overlay.querySelector('.ft-confirm-cancel');
      var okBtn = overlay.querySelector('.ft-confirm-ok');

      input.focus();
      input.select();

      function close(value){
        overlay.style.animation = 'ft-out 120ms ease-in forwards';
        setTimeout(function(){ overlay.remove(); }, 130);
        resolve(value);
      }

      cancelBtn.addEventListener('click', function(){ close(null); });
      okBtn.addEventListener('click', function(){ close(input.value); });
      input.addEventListener('keydown', function(e){ if(e.key === 'Enter') close(input.value); if(e.key === 'Escape') close(null); });
      overlay.addEventListener('click', function(e){ if(e.target === overlay) close(null); });
    });
  };
})();
