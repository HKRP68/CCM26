/* Progressive enhancement; the forms also work without JavaScript. */
(() => {
  'use strict';
  const root=document.querySelector('.au-team-manager');
  if(!root) return;
  const purse=Number(root.dataset.purse), symbol=root.dataset.currency;
  const money=n=>`${n<0?'−':''}${symbol}${Math.abs(n)>=100?(Math.abs(n)/100).toLocaleString('en-IN',{maximumFractionDigits:2})+' Cr':Math.abs(n)+' L'}`;
  const balance=form=>{
    const amount=Number(form.elements.amount_lakh.value);
    return form.elements.mode.value==='set'?amount:purse+(form.elements.mode.value==='deduct'?-amount:amount);
  };
  function preview(form) {
    const kind=form.dataset.review;
    if(kind==='purse') {
      const next=balance(form), output=document.getElementById('purse-preview');
      output.textContent=`${money(purse)} → ${money(next)}${next<0?' · Balance cannot be negative':''}`;
      output.classList.toggle('au-debit',next<0);
    }
    if(kind==='sign') {
      const player=form.querySelector('input[name="player_id"]:checked');
      if(!player) return;
      const next=purse-Number(form.elements.price_lakh.value);
      const output=document.getElementById('signing-preview');
      output.textContent=`${player.dataset.name} · ${money(purse)} → ${money(next)}${next<0?' · Insufficient purse':''}`;
      output.classList.toggle('au-debit',next<0);
    }
  }
  root.addEventListener('change',e=>{
    const form=e.target.closest('.au-review-form');
    if(!form) return;
    if(e.target.name==='player_id') form.elements.price_lakh.value=e.target.dataset.price;
    preview(form);
  });
  root.addEventListener('input',e=>{const f=e.target.closest('.au-review-form');if(f)preview(f);});
  root.addEventListener('submit',e=>{
    const f=e.target.closest('.au-review-form');if(!f)return;
    if(f.dataset.submitting) {e.preventDefault();return;}
    let message='';
    if(f.dataset.review==='purse') message=`Change this team's purse from ${money(purse)} to ${money(balance(f))}?`;
    if(f.dataset.review==='sign') message=`Sign ${f.querySelector('input[name="player_id"]:checked')?.dataset.name} for ${money(Number(f.elements.price_lakh.value))}?`;
    if(f.dataset.review==='remove') message=`Remove ${f.dataset.player} and refund ${money(Number(f.dataset.refund))}? The player returns to the auction queue.`;
    if(!window.confirm(message)) {e.preventDefault();return;}
    f.dataset.submitting='1';
    f.querySelectorAll('button[type="submit"]').forEach(b=>{b.disabled=true;b.textContent='Saving…';});
  });
})();
