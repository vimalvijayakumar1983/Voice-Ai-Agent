import { FormEvent, useEffect, useMemo, useState } from 'react';
import {
  Bot, CheckCircle2, ChevronRight, CircleAlert, CreditCard, Loader2,
  LockKeyhole, PackageCheck, Search, ShieldCheck, ShoppingCart, Sparkles,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, CommerceProviderStatus, CommerceSession, VoiceAgent } from '@/lib/api';
import styles from '@/styles/Commerce.module.css';

type Product = { name: string; product_path: string; price?: string | null; delivery?: string | null };

function message(error: unknown) {
  return error instanceof Error ? error.message : 'The browser action could not be completed.';
}

function latestProducts(session: CommerceSession | null): Product[] {
  const searchAction = [...(session?.actions ?? [])].reverse().find((item) => item.action_type === 'search');
  const products = searchAction?.result_summary.products;
  return Array.isArray(products) ? products as Product[] : [];
}

export default function Commerce() {
  const [status, setStatus] = useState<CommerceProviderStatus | null>(null);
  const [sessions, setSessions] = useState<CommerceSession[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [active, setActive] = useState<CommerceSession | null>(null);
  const [query, setQuery] = useState('');
  const [agentId, setAgentId] = useState('');
  const [working, setWorking] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  const [checkout, setCheckout] = useState({
    first_name: '', last_name: '', phone: '', email: '', address_line_1: '',
    address_line_2: '', city: 'Abu Dhabi', emirate: 'Abu Dhabi', landmark: '',
    payment_method: 'cod',
  });

  const products = useMemo(() => latestProducts(active), [active]);

  useEffect(() => {
    Promise.all([api.getCommerceStatus(), api.listCommerceSessions(), api.listAgents()])
      .then(([provider, items, agentItems]) => {
        setStatus(provider); setSessions(items); setAgents(agentItems); setActive(items[0] ?? null);
      })
      .catch((reason) => setError(message(reason)));
  }, []);

  const updateSession = (next: CommerceSession) => {
    setActive(next);
    setSessions((items) => [next, ...items.filter((item) => item.id !== next.id)]);
  };

  const createSession = async () => {
    setWorking('create'); setError(''); setNotice('');
    try {
      const next = await api.createCommerceSession(agentId || undefined);
      updateSession(next); setNotice('A private two-hour shopping session is ready.');
    } catch (reason) { setError(message(reason)); } finally { setWorking(''); }
  };

  const runSearch = async (event: FormEvent) => {
    event.preventDefault();
    if (!active || query.trim().length < 2) return;
    setWorking('search'); setError(''); setNotice('');
    try { updateSession(await api.searchCommerce(active.id, query.trim())); }
    catch (reason) { setError(message(reason)); } finally { setWorking(''); }
  };

  const addProduct = async (product: Product) => {
    if (!active) return;
    setWorking(product.product_path); setError('');
    try {
      updateSession(await api.addCommerceCartItem(active.id, product.product_path));
      setNotice(`${product.name} was added to the isolated FEPY cart.`);
    } catch (reason) { setError(message(reason)); } finally { setWorking(''); }
  };

  const prepareCheckout = async (event: FormEvent) => {
    event.preventDefault();
    if (!active) return;
    setWorking('checkout'); setError(''); setNotice('');
    const { payment_method, ...customer } = checkout;
    try {
      updateSession(await api.prepareCommerceCheckout(active.id, { payment_method, customer }));
      setCheckoutOpen(false);
      setNotice('Checkout prepared. Read the summary to the customer before confirmation.');
    } catch (reason) { setError(message(reason)); } finally { setWorking(''); }
  };

  const confirm = async () => {
    if (!active) return;
    setWorking('confirm'); setError('');
    try {
      updateSession(await api.confirmCommerceOrder(active.id));
      setNotice('Explicit customer confirmation recorded.');
    } catch (reason) { setError(message(reason)); } finally { setWorking(''); }
  };

  const cartTotal = String(active?.cart_snapshot.total_including_vat ?? '—');
  const itemCount = String(active?.cart_snapshot.item_count ?? '0');

  return (
    <Layout>
      <div className={styles.page}>
        <header className={styles.hero}>
          <div><span className={styles.eyebrow}>VOICE COMMERCE CONTROL</span><h1>FEPY shopping agent</h1>
            <p>Live website data, isolated carts and confirmation-gated checkout—without exposing card details.</p></div>
          <div className={styles.provider}><span className={status?.enabled ? styles.dotOn : styles.dotOff} />
            <div><strong>{status?.enabled ? 'Browser engine ready' : 'Browser engine disabled'}</strong>
              <small>COD submission {status?.order_submission_enabled ? 'enabled' : 'safety locked'}</small></div></div>
        </header>

        {error && <div className={styles.error}><CircleAlert size={17} />{error}</div>}
        {notice && <div className={styles.success}><CheckCircle2 size={17} />{notice}</div>}

        <section className={styles.metrics} aria-label="Commerce safeguards">
          <article><Search /><div><strong>Live catalogue</strong><span>Price, stock and ETA from FEPY</span></div></article>
          <article><LockKeyhole /><div><strong>Private sessions</strong><span>Encrypted cart and customer context</span></div></article>
          <article><ShieldCheck /><div><strong>Confirmation gate</strong><span>No order without explicit approval</span></div></article>
          <article><CreditCard /><div><strong>PCI-safe cards</strong><span>Hosted checkout only</span></div></article>
        </section>

        <div className={styles.workspace}>
          <aside className={styles.sidebar}>
            <div className={styles.sideTitle}><div><span>Sessions</span><strong>{sessions.length}</strong></div>
              <select aria-label="Agent for new commerce session" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                <option value="">No agent selected</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
              </select>
              <button className={styles.primary} onClick={createSession} disabled={!!working}>
                {working === 'create' ? <Loader2 className={styles.spin} /> : <Sparkles />}New shopping session</button>
            </div>
            <div className={styles.sessionList}>{sessions.map((session) => (
              <button key={session.id} onClick={() => setActive(session)} className={active?.id === session.id ? styles.activeSession : ''}>
                <ShoppingCart /><div><strong>{session.channel.replace('_', ' ')}</strong><span>{session.status} · {new Date(session.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span></div><ChevronRight />
              </button>
            ))}</div>
          </aside>

          <main className={styles.main}>
            {!active ? <div className={styles.empty}><Bot /><h2>Start a secure shopping session</h2><p>Each customer receives an isolated cart that expires automatically.</p></div> : <>
              <div className={styles.sessionHeader}><div><span>SESSION {active.id.slice(0, 8).toUpperCase()}</span><h2>Guided purchase workspace</h2></div><span className={styles.status}>{active.status.replace('_', ' ')}</span></div>
              <form className={styles.searchBar} onSubmit={runSearch}><Search /><input aria-label="Search FEPY products" placeholder="Ask for a product, brand or specification…" value={query} onChange={(e) => setQuery(e.target.value)} /><button disabled={working === 'search'}>{working === 'search' ? <Loader2 className={styles.spin} /> : 'Search live FEPY'}</button></form>

              {products.length > 0 && <section className={styles.products}><div className={styles.sectionTitle}><h3>Live results</h3><span>{products.length} products</span></div>{products.map((product) => (
                <article key={product.product_path}><div className={styles.productIcon}><PackageCheck /></div><div><strong>{product.name}</strong><span>{product.delivery || 'Delivery checked on product page'}</span></div><div className={styles.productAction}><strong>{product.price || 'Check price'}</strong><button onClick={() => addProduct(product)} disabled={!!working}>{working === product.product_path ? <Loader2 className={styles.spin} /> : 'Add'}</button></div></article>
              ))}</section>}

              <section className={styles.cart}><div className={styles.sectionTitle}><h3>Verified cart</h3><span>Visible FEPY total</span></div><div className={styles.cartSummary}><div><span>Items</span><strong>{itemCount}</strong></div><div><span>Total including VAT</span><strong>AED {cartTotal}</strong></div><button onClick={() => setCheckoutOpen(true)} disabled={!active.cart_snapshot.source || active.status === 'confirmed'}>Prepare checkout</button></div></section>

              {active.status === 'awaiting_confirmation' && <section className={styles.confirm}><ShieldCheck /><div><h3>Customer confirmation required</h3><p>Read back the items, VAT-inclusive total, address, delivery method and payment method. Continue only after the customer clearly says “Confirm order”.</p></div><button onClick={confirm} disabled={working === 'confirm'}>{working === 'confirm' ? <Loader2 className={styles.spin} /> : 'Record “Confirm order”'}</button></section>}
              {active.status === 'confirmed' && <section className={styles.locked}><CheckCircle2 /><div><h3>Confirmation captured</h3><p>COD/store pickup submission remains safety locked until checkout acceptance tests pass. Hosted-card purchases continue on FEPY’s secure checkout.</p></div></section>}
            </>}
          </main>
        </div>

        {checkoutOpen && active && <div className={styles.overlay} role="presentation"><form className={styles.modal} onSubmit={prepareCheckout} aria-label="Prepare secure checkout"><div className={styles.modalHead}><div><span>SECURE CHECKOUT PREPARATION</span><h2>Delivery and payment</h2></div><button type="button" onClick={() => setCheckoutOpen(false)}>×</button></div><p className={styles.safety}>Never enter card number, CVV, PIN or OTP. Card customers are transferred to hosted FEPY checkout.</p><div className={styles.formGrid}>
          {(['first_name','last_name','phone','email','address_line_1','address_line_2','city','landmark'] as const).map((field) => <label key={field}><span>{field.replaceAll('_', ' ')}</span><input required={!['address_line_2','landmark'].includes(field)} value={checkout[field]} onChange={(e) => setCheckout({ ...checkout, [field]: e.target.value })} /></label>)}
          <label><span>Emirate</span><select value={checkout.emirate} onChange={(e) => setCheckout({ ...checkout, emirate: e.target.value })}>{['Abu Dhabi','Dubai','Sharjah','Ajman','Umm Al Quwain','Ras Al Khaimah','Fujairah'].map((name) => <option key={name}>{name}</option>)}</select></label>
          <label><span>Payment method</span><select value={checkout.payment_method} onChange={(e) => setCheckout({ ...checkout, payment_method: e.target.value })}><option value="cod">Cash on delivery</option><option value="store_pickup">Store pickup</option><option value="hosted_card">Secure card checkout</option></select></label>
        </div><button className={styles.submit} disabled={working === 'checkout'}>{working === 'checkout' ? <Loader2 className={styles.spin} /> : 'Prepare confirmation summary'}</button></form></div>}
      </div>
    </Layout>
  );
}
