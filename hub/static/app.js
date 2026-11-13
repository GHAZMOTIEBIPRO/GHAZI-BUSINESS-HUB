'use strict';
/* Progressive enhancement only: every page works without JS except the live calculator. */
(function () {
  var btn = document.getElementById('menuBtn');
  var nav = document.getElementById('siteNav');
  if (btn && nav) {
    btn.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // phones: show results first; the filter panel opens on demand (stays open when a filter is active)
  var fb = document.getElementById('filterBox');
  if (fb && !fb.dataset.active && window.matchMedia('(max-width: 700px)').matches) fb.removeAttribute('open');

  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // quote editor: add a blank row (server still caps at 25 rows)
  var add = document.getElementById('addLine');
  if (add) {
    add.addEventListener('click', function () {
      var body = document.querySelector('#qlines tbody');
      var rows = body.querySelectorAll('tr');
      if (rows.length >= Number(add.dataset.max || 25)) return;
      var clone = rows[rows.length - 1].cloneNode(true);
      clone.querySelectorAll('input').forEach(function (i) { i.value = ''; });
      body.appendChild(clone);
    });
  }

  // ---------------------------------------------------------------- calculator
  var form = document.getElementById('calcForm');
  if (!form) return;
  var fmt = new Intl.NumberFormat('ar-SA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var out = document.getElementById('calcOut');
  var warn = document.getElementById('calcWarning');
  var copyBtn = document.getElementById('copyQuote');
  var lastQuote = '';
  var ids = ['directCost', 'sellPrice', 'overheadValue', 'profitValue', 'vatValue', 'grandTotal'];

  function clear(msg) {
    ids.forEach(function (id) { document.getElementById(id).textContent = '—'; });
    out.classList.add('is-invalid');
    warn.textContent = msg;
    warn.className = 'error';
    lastQuote = '';
    copyBtn.disabled = true;
  }

  function calc() {
    var f = new FormData(form);
    var n = function (k) { var v = String(f.get(k) || '').trim(); return v === '' ? NaN : Number(v); };
    var qty = n('qty'), mat = n('materials'), lab = n('labor');
    var oh = n('overhead') / 100, mg = n('margin') / 100, vat = n('vat') / 100;
    var vals = [qty, mat, lab, oh, mg, vat];
    if (vals.some(function (x) { return !isFinite(x) || x < 0; })) return clear('أدخل أرقامًا صحيحة غير سالبة في كل الحقول.');
    if (oh + mg >= 1) return clear('مجموع هامش الربح والمصاريف العامة يجب أن يكون أقل من 100%.');
    var direct = qty * (mat + lab), sell = direct / (1 - oh - mg);
    var r = { directCost: direct, sellPrice: sell, overheadValue: sell * oh, profitValue: sell * mg, vatValue: sell * vat,
      grandTotal: sell * (1 + vat) };
    ids.forEach(function (id) { document.getElementById(id).textContent = fmt.format(r[id]) + ' ر.س'; });
    out.classList.remove('is-invalid');
    warn.textContent = 'تقدير أولي لا يغني عن الحصر وجداول الكميات.';
    warn.className = 'muted small';
    copyBtn.disabled = false;
    lastQuote = ['البند: ' + f.get('description'), 'الكمية: ' + qty + ' ' + f.get('unit'),
      'التكلفة المباشرة: ' + fmt.format(direct) + ' ر.س', 'سعر العرض قبل الضريبة: ' + fmt.format(sell) + ' ر.س',
      'الضريبة: ' + fmt.format(r.vatValue) + ' ر.س', 'الإجمالي: ' + fmt.format(r.grandTotal) + ' ر.س'].join('\n');
  }
  form.addEventListener('input', calc);
  form.addEventListener('submit', function (e) { e.preventDefault(); calc(); });
  copyBtn.addEventListener('click', function () {
    if (!lastQuote) return;
    navigator.clipboard.writeText(lastQuote).then(function () { copyBtn.textContent = 'نُسخ ✓'; },
      function () { copyBtn.textContent = 'النسخ غير متاح في هذا المتصفح'; });
  });
  calc();
})();
