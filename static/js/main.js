document.addEventListener('DOMContentLoaded', function() {
  initializeCountdown();
  initializeReferralCopy();
  initializeFormValidations();
});

function initializeCountdown() {
  const countdownElement = document.getElementById('income-countdown');
  if (!countdownElement) return;
  const targetTime = new Date(countdownElement.dataset.target).getTime();
  const countdown = setInterval(function() {
    const now = new Date().getTime();
    const distance = targetTime - now;
    const hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
    const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((distance % (1000 * 60)) / 1000);
    countdownElement.innerHTML = `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
    if (distance < 0) {
      clearInterval(countdown);
      countdownElement.innerHTML = "Income Ready!";
      countdownElement.classList.add('text-success');
    }
  }, 1000);
}

function initializeReferralCopy() {
  const copyButton = document.getElementById('copy-referral');
  if (!copyButton) return;
  copyButton.addEventListener('click', function() {
    const referralLink = document.getElementById('referral-link').value;
    navigator.clipboard.writeText(referralLink).then(function() {
      const originalText = copyButton.innerHTML;
      copyButton.innerHTML = '<i class="fas fa-check"></i> Copied!';
      copyButton.classList.add('btn-success');
      setTimeout(function() {
        copyButton.innerHTML = originalText;
        copyButton.classList.remove('btn-success');
      }, 2000);
    });
  });
}

function initializeFormValidations() {
  const withdrawalForm = document.getElementById('withdrawal-form');
  if (withdrawalForm) {
    withdrawalForm.addEventListener('submit', function(event) {
      const amount = parseFloat(document.getElementById('amount').value);
      const minAmount = parseFloat(withdrawalForm.dataset.minimum || 1000);
      if (amount < minAmount) {
        event.preventDefault();
        showAlert(`Minimum withdrawal amount is ₦${minAmount}`, 'danger');
        return false;
      }
    });
  }
}

function showAlert(message, type) {
  if (window.showToast) {
    window.showToast(message, type || 'info');
  }
}
