(function(){
  if (window.FinTechAlertsLoaded) return;
  window.FinTechAlertsLoaded = true;

  const styles = `
  .ft-toast-container { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 20000; display: flex; flex-direction: column; gap: 10px; width: min(92vw, 420px); }
  .ft-toast { display: flex; align-items: start; gap: 10px; padding: 12px 14px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); color: #F8FAFC; background: #20232A; border: 1px solid #2C313A; border-left: 4px solid #D4AF37; opacity: 0; transform: translateY(-8px); animation: ft-toast-in 180ms ease-out forwards; }
  .ft-toast.success { border-left-color: #22C55E; }
  .ft-toast.warning { border-left-color: #F59E0B; }
  .ft-toast.danger { border-left-color: #EF4444; }
  .ft-toast .ft-icon { font-size: 18px; margin-top: 2px; }
  .ft-toast .ft-content { flex: 1; }
  .ft-toast .ft-title { font-weight: 700; font-size: 14px; margin: 0 0 2px; color: #F8FAFC; }
  .ft-toast .ft-message { font-size: 13px; margin: 0; color: #B3B8C4; }
  .ft-toast .ft-close { background: transparent; border: 0; color: #B3B8C4; opacity: .7; cursor: pointer; font-size: 18px; line-height: 1; padding: 0 2px; }
  .ft-toast .ft-close:hover { opacity: 1; }
  @keyframes ft-toast-in { to { opacity: 1; transform: translateY(0); } }
  @keyframes ft-toast-out { to { opacity: 0; transform: translateY(-6px); } }
  `;

  const styleEl = document.createElement('style');
  styleEl.textContent = styles;
  document.head.appendChild(styleEl);

  function ensureContainer(){
    let c = document.querySelector('.ft-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.className = 'ft-toast-container';
      document.body.appendChild(c);
    }
    return c;
  }

  function iconFor(type){
    switch(type){
      case 'success': return '<i class="fas fa-check-circle ft-icon" style="color:#22C55E"></i>';
      case 'warning': return '<i class="fas fa-exclamation-triangle ft-icon" style="color:#F59E0B"></i>';
      case 'danger': return '<i class="fas fa-times-circle ft-icon" style="color:#EF4444"></i>';
      default: return '<i class="fas fa-info-circle ft-icon" style="color:#D4AF37"></i>';
    }
  }

  window.showToast = function(message, type = 'info', options = {}){
    const { title = null, timeout = 3500 } = options;
    const container = ensureContainer();

    const toast = document.createElement('div');
    toast.className = `ft-toast ${type}`;
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');

    toast.innerHTML = `
      ${iconFor(type)}
      <div class="ft-content">
        ${title ? `<div class="ft-title">${title}</div>` : ''}
        <div class="ft-message">${message}</div>
      </div>
      <button class="ft-close" aria-label="Close">&times;</button>
    `;

    const closeBtn = toast.querySelector('.ft-close');
    closeBtn.addEventListener('click', () => dismiss());

    container.appendChild(toast);

    let hideTimer;
    if (timeout > 0) {
      hideTimer = setTimeout(() => dismiss(), timeout);
    }

    function dismiss(){
      if (hideTimer) clearTimeout(hideTimer);
      toast.style.animation = 'ft-toast-out 160ms ease-in forwards';
      setTimeout(() => toast.remove(), 170);
    }

    return { dismiss };
  };
})();
