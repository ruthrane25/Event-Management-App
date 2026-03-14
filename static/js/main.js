/* =========================================================
   EventFlow — Shared JS Utilities
   ========================================================= */

// ── Toast Notifications ───────────────────────────────────
let toastContainer = null;

function getToastContainer() {
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container';
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

function showToast(message, type = 'info', duration = 3000) {
  const container = getToastContainer();
  const iconMap = { success: 'check-circle', error: 'exclamation-circle', info: 'info-circle' };
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<i class="fas fa-${iconMap[type] || 'info-circle'}"></i> ${message}`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ── Auto-dismiss Flash Messages ───────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // auto-dismiss flash messages after 5 seconds
  document.querySelectorAll('.flash').forEach(flash => {
    setTimeout(() => {
      flash.style.opacity = '0';
      flash.style.transform = 'translateX(100%)';
      flash.style.transition = 'all 0.4s ease';
      setTimeout(() => flash.remove(), 400);
    }, 5000);
  });

  // Close modal on backdrop click
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.classList.add('hidden');
    });
  });

  // Close modal on Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(m => {
        m.classList.add('hidden');
      });
    }
  });
});

// ── Ripple effect on buttons ──────────────────────────────
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.btn');
  if (!btn) return;
  const circle = document.createElement('span');
  const diameter = Math.max(btn.clientWidth, btn.clientHeight);
  const radius = diameter / 2;
  const rect = btn.getBoundingClientRect();
  circle.style.cssText = `
    width:${diameter}px; height:${diameter}px;
    left:${e.clientX - rect.left - radius}px;
    top:${e.clientY - rect.top - radius}px;
    position:absolute; border-radius:50%;
    background:rgba(255,255,255,0.2);
    transform:scale(0); animation:ripple 0.5s linear;
    pointer-events:none;
  `;
  btn.style.position = 'relative';
  btn.style.overflow = 'hidden';
  btn.appendChild(circle);
  setTimeout(() => circle.remove(), 500);
});

// Add ripple keyframes dynamically
const style = document.createElement('style');
style.textContent = `@keyframes ripple { to { transform:scale(2.5); opacity:0; } }`;
document.head.appendChild(style);
