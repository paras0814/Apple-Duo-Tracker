(async () => {
  const d = document;
  const L = location;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  if (!['www.apple.com', 'apple.com'].includes(L.hostname)) {
    return alert('Open the exact iPhone Duo model from your alert on apple.com, then run this shortcut.');
  }
  if (L.pathname.startsWith('/shop/bag')) {
    return alert('Your Apple bag is open. Review the model and pickup store, then check out with Apple Pay.');
  }
  if (!L.pathname.startsWith('/shop/buy-iphone/iphone-duo')) {
    return alert('Open an iPhone Duo model page on apple.com, then run this shortcut.');
  }
  const slugs = {'256gb-star-white': 'MK1E4LL/A', '256gb-night-sky': 'MK1F4LL/A'};
  const hash = L.hash.match(/M[A-Z0-9]{4}LL\/A/i);
  const queryPart = new URLSearchParams(L.search).get('product');
  const field = [...d.querySelectorAll('input[name="product"]')].find(input => input.value);
  const slug = Object.keys(slugs).find(value => L.pathname.includes(value));
  const part = ((hash && hash[0]) || queryPart || (field && field.value) || (slug && slugs[slug]) || '').toUpperCase();
  if (!Object.values(slugs).includes(part)) {
    return alert('This shortcut supports the two monitored 256GB models. Open the exact model link from your Discord alert.');
  }
  let token;
  for (let attempt = 0; attempt < 40 && !token; attempt++) {
    try {
      token = decodeURIComponent((d.cookie.split('; ').find(cookie => cookie.startsWith('as_atb=')) || '').slice(7)).split('|')[2];
    } catch { token = undefined; }
    if (!token) await wait(150);
  }
  if (!token) return alert('Apple has not finished loading this page. Wait a moment, then run the shortcut again.');
  const banner = d.createElement('div');
  banner.textContent = 'Adding 256GB Duo to your Apple bag…';
  banner.style.cssText = 'position:fixed;top:14px;left:0;right:0;margin:auto;width:fit-content;z-index:2147483647;background:black;color:white;padding:10px 18px;border-radius:999px;font:600 15px -apple-system,sans-serif';
  d.body.appendChild(banner);
  try {
    const params = new URLSearchParams({product: part, purchaseOption: 'fullPrice', cppart: 'UNLOCKED/US',
      step: 'select', acpart: 'none', bfil: '2', atbtoken: token, 'add-to-cart': 'add-to-cart'});
    const response = await fetch('/shop/buy-iphone/iphone-duo?' + params, {credentials: 'include'});
    if (response.ok && new URL(response.url).searchParams.get('step') === 'attach') {
      L.href = '/shop/bag';
    } else {
      alert('Apple did not confirm the add. Use Add to Bag on the product page.');
    }
  } catch {
    alert('The Apple request did not finish. Check your bag before trying again, or use Add to Bag on the page.');
  } finally {
    banner.remove();
  }
})();
