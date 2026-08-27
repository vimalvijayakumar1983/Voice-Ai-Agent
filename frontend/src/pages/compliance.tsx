import { useState } from 'react';
import Layout from '@/components/Layout';
import { api } from '@/lib/api';

export default function Compliance() {
  const [checkNumber, setCheckNumber] = useState('');
  const [checkResult, setCheckResult] = useState<{ is_on_dnc: boolean } | null>(null);
  const [addNumber, setAddNumber] = useState('');
  const [addReason, setAddReason] = useState('');

  const handleCheck = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const result = await api.checkDnc(checkNumber);
      setCheckResult(result);
    } catch (error: unknown) {
      alert(error instanceof Error ? error.message : 'Could not check the DNC list.');
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.addDnc({ phone_number: addNumber, reason: addReason || undefined });
      setAddNumber('');
      setAddReason('');
      alert('Number added to DNC list');
    } catch (error: unknown) {
      alert(error instanceof Error ? error.message : 'Could not update the DNC list.');
    }
  };

  return (
    <Layout>
      <div className="page-header">
        <h1>Compliance</h1>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Check DNC List</h3>
          <form onSubmit={handleCheck}>
            <div className="form-group">
              <label>Phone Number</label>
              <input value={checkNumber} onChange={(e) => setCheckNumber(e.target.value)}
                placeholder="+1234567890" required />
            </div>
            <button type="submit" className="btn btn-primary">Check</button>
          </form>
          {checkResult !== null && (
            <div style={{ marginTop: 12, padding: 12, background: 'var(--bg-primary)', borderRadius: 6 }}>
              <span className={`badge ${checkResult.is_on_dnc ? 'badge-danger' : 'badge-success'}`}>
                {checkResult.is_on_dnc ? 'ON DNC LIST' : 'NOT ON DNC LIST'}
              </span>
            </div>
          )}
        </div>

        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Add to DNC List</h3>
          <form onSubmit={handleAdd}>
            <div className="form-group">
              <label>Phone Number</label>
              <input value={addNumber} onChange={(e) => setAddNumber(e.target.value)}
                placeholder="+1234567890" required />
            </div>
            <div className="form-group">
              <label>Reason</label>
              <select value={addReason} onChange={(e) => setAddReason(e.target.value)}>
                <option value="">Select reason</option>
                <option value="customer_request">Customer Request</option>
                <option value="regulatory">Regulatory</option>
                <option value="complaint">Complaint</option>
              </select>
            </div>
            <button type="submit" className="btn btn-danger">Add to DNC</button>
          </form>
        </div>
      </div>

      <div className="card" style={{ marginTop: 24 }}>
        <h3 style={{ marginBottom: 12 }}>Compliance Features</h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
          <div style={{ padding: 16, background: 'var(--bg-primary)', borderRadius: 6 }}>
            <h4 style={{ fontSize: 14, marginBottom: 4 }}>DNC Management</h4>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Automatic DNC checking before outbound calls</p>
          </div>
          <div style={{ padding: 16, background: 'var(--bg-primary)', borderRadius: 6 }}>
            <h4 style={{ fontSize: 14, marginBottom: 4 }}>Consent Tracking</h4>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Record and manage call recording consent</p>
          </div>
          <div style={{ padding: 16, background: 'var(--bg-primary)', borderRadius: 6 }}>
            <h4 style={{ fontSize: 14, marginBottom: 4 }}>Calling Hours</h4>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Enforce calling hours per timezone</p>
          </div>
        </div>
      </div>
    </Layout>
  );
}
