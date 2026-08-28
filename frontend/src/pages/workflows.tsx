import { FormEvent, useEffect, useState } from 'react';
import Link from 'next/link';
import {
  Bot,
  CheckCircle2,
  CircleAlert,
  GitBranch,
  Loader2,
  MessageCircle,
  Pencil,
  PhoneForwarded,
  PhoneOff,
  Plus,
  RotateCw,
  Save,
  Trash2,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import {
  api,
  CallWorkflow,
  VoiceAgent,
  WorkflowNodeInput,
  WorkflowNodeType,
  WorkflowTrigger,
} from '@/lib/api';

type WorkflowTemplate = 'assistant' | 'handoff';

interface WorkflowForm {
  name: string;
  description: string;
  agentId: string;
  trigger: WorkflowTrigger;
  template: WorkflowTemplate;
  greeting: string;
  transferNumber: string;
}

interface PageNotice {
  type: 'success' | 'error';
  text: string;
}

interface TemplateDefinition {
  id: WorkflowTemplate;
  name: string;
  description: string;
  nodeTypes: WorkflowNodeType[];
}

const TEMPLATES: TemplateDefinition[] = [
  {
    id: 'assistant',
    name: 'AI conversation',
    description: 'Greet the caller, run an AI conversation, then close the call.',
    nodeTypes: ['greeting', 'ai_conversation', 'hangup'],
  },
  {
    id: 'handoff',
    name: 'AI with human handoff',
    description: 'Greet, handle with AI, transfer to a verified number, then close.',
    nodeTypes: ['greeting', 'ai_conversation', 'transfer', 'hangup'],
  },
];

const TRIGGER_LABELS: Record<WorkflowTrigger, string> = {
  inbound_call: 'Inbound call',
  campaign: 'Campaign',
  api: 'API request',
};

const NODE_LABELS: Record<WorkflowNodeType, string> = {
  greeting: 'Greeting',
  gather_input: 'Gather input',
  ai_conversation: 'AI conversation',
  transfer: 'Human transfer',
  hangup: 'Hang up',
  condition: 'Condition',
  webhook: 'Webhook',
};

const E164_PATTERN = /^\+[1-9]\d{7,14}$/;

function blankForm(agentId = ''): WorkflowForm {
  return {
    name: '',
    description: '',
    agentId,
    trigger: 'inbound_call',
    template: 'assistant',
    greeting: 'Hello! How can I help you today?',
    transferNumber: '',
  };
}

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function configString(config: Record<string, unknown>, key: string) {
  const value = config[key];
  return typeof value === 'string' ? value : '';
}

function buildNodes(form: WorkflowForm): WorkflowNodeInput[] {
  const nodes: WorkflowNodeInput[] = [
    {
      position: 0,
      node_type: 'greeting',
      config: { message: form.greeting.trim() },
      next_node_id: null,
    },
    {
      position: 1,
      node_type: 'ai_conversation',
      config: { agent_id: form.agentId, max_duration: 300 },
      next_node_id: null,
    },
  ];
  if (form.template === 'handoff') {
    nodes.push({
      position: nodes.length,
      node_type: 'transfer',
      config: {
        number: form.transferNumber.trim(),
        whisper: 'Transferring the caller to a team member.',
      },
      next_node_id: null,
    });
  }
  nodes.push({
    position: nodes.length,
    node_type: 'hangup',
    config: { message: 'Thank you for calling. Goodbye.' },
    next_node_id: null,
  });
  return nodes;
}

function linkNodes(workflow: CallWorkflow): WorkflowNodeInput[] {
  return workflow.nodes.map((node, index) => ({
    position: node.position,
    node_type: node.node_type,
    config: node.config,
    next_node_id: workflow.nodes[index + 1]?.id ?? null,
  }));
}

function templateFor(workflow: CallWorkflow): WorkflowTemplate | null {
  if (workflow.nodes.length === 0) return 'assistant';
  const nodeTypes = [...workflow.nodes]
    .sort((left, right) => left.position - right.position)
    .map((node) => node.node_type);
  const matches = (template: TemplateDefinition) => (
    template.nodeTypes.length === nodeTypes.length
    && template.nodeTypes.every((nodeType, index) => nodeType === nodeTypes[index])
  );
  return TEMPLATES.find(matches)?.id ?? null;
}

function nodeIcon(nodeType: WorkflowNodeType) {
  if (nodeType === 'greeting') return <MessageCircle size={13} />;
  if (nodeType === 'ai_conversation') return <Bot size={13} />;
  if (nodeType === 'transfer') return <PhoneForwarded size={13} />;
  if (nodeType === 'hangup') return <PhoneOff size={13} />;
  return <GitBranch size={13} />;
}

export default function Workflows() {
  const [workflows, setWorkflows] = useState<CallWorkflow[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState<PageNotice | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<WorkflowForm>(() => blankForm());
  const [formError, setFormError] = useState('');
  const [working, setWorking] = useState('');

  useEffect(() => {
    let active = true;
    Promise.all([api.listWorkflows(), api.listAgents()])
      .then(([workflowItems, agentItems]) => {
        if (!active) return;
        setWorkflows(workflowItems);
        setAgents(agentItems);
        setLoadError('');
      })
      .catch((error) => {
        if (active) setLoadError(messageFrom(error, 'Could not load workflow drafts.'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [reloadKey]);

  const retryLoad = () => {
    setLoading(true);
    setLoadError('');
    setReloadKey((current) => current + 1);
  };

  const openCreate = () => {
    setEditingId(null);
    setForm(blankForm(agents[0]?.id));
    setFormError('');
    setNotice(null);
    setShowForm(true);
  };

  const openEditForm = (workflow: CallWorkflow) => {
    const selectedTemplate = templateFor(workflow) ?? 'assistant';
    const greetingNode = workflow.nodes.find((node) => node.node_type === 'greeting');
    const conversationNode = workflow.nodes.find((node) => node.node_type === 'ai_conversation');
    const transferNode = workflow.nodes.find((node) => node.node_type === 'transfer');
    setEditingId(workflow.id);
    setForm({
      name: workflow.name,
      description: workflow.description ?? '',
      agentId: workflow.agent_id
        ?? (conversationNode ? configString(conversationNode.config, 'agent_id') : ''),
      trigger: workflow.trigger_type,
      template: selectedTemplate,
      greeting: greetingNode ? configString(greetingNode.config, 'message') : '',
      transferNumber: transferNode ? configString(transferNode.config, 'number') : '',
    });
    setFormError('');
    setNotice(null);
    setShowForm(true);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const editWorkflow = async (workflow: CallWorkflow) => {
    if (!templateFor(workflow)) {
      setNotice({
        type: 'error',
        text: `${workflow.name} uses advanced nodes that this template editor cannot safely change. Its configuration was left untouched.`,
      });
      return;
    }
    if (!workflow.is_active) {
      openEditForm(workflow);
      return;
    }
    if (!window.confirm(`Deactivate ${workflow.name} and open it for editing?`)) return;

    setWorking(`deactivate-${workflow.id}`);
    try {
      const updated = await api.updateWorkflow(workflow.id, { is_active: false });
      setWorkflows((current) => current.map((item) => item.id === updated.id ? updated : item));
      openEditForm(updated);
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not deactivate the workflow.') });
    } finally {
      setWorking('');
    }
  };

  const closeForm = () => {
    setShowForm(false);
    setEditingId(null);
    setForm(blankForm(agents[0]?.id));
    setFormError('');
  };

  const saveWorkflow = async (event: FormEvent) => {
    event.preventDefault();
    setFormError('');
    setNotice(null);

    const name = form.name.trim();
    if (name.length < 2) {
      setFormError('Workflow name must contain at least two characters.');
      return;
    }
    if (!agents.some((agent) => agent.id === form.agentId)) {
      setFormError('Choose an agent from this workspace.');
      return;
    }
    if (!form.greeting.trim()) {
      setFormError('Add a greeting for the first node.');
      return;
    }
    if (form.template === 'handoff' && !E164_PATTERN.test(form.transferNumber.trim())) {
      setFormError('Enter the transfer number in E.164 format, for example +971501234567.');
      return;
    }

    setWorking(editingId ? `save-${editingId}` : 'create');
    try {
      const payload = {
        name,
        description: form.description.trim() || null,
        agent_id: form.agentId,
        trigger_type: form.trigger,
        config: { template: form.template, runtime_status: 'configuration_only' },
        nodes: buildNodes(form),
      };
      let saved = editingId
        ? await api.updateWorkflow(editingId, payload)
        : await api.createWorkflow({ ...payload, is_active: false });

      // Node IDs are server-owned. Persist the draft once to receive them, then
      // connect the validated linear graph in a second tenant-scoped update.
      saved = await api.updateWorkflow(saved.id, { nodes: linkNodes(saved) });
      setWorkflows((current) => (
        editingId
          ? current.map((item) => item.id === saved.id ? saved : item)
          : [saved, ...current]
      ));
      setNotice({
        type: 'success',
        text: `${saved.name} was saved as a configuration draft. No calls were started.`,
      });
      closeForm();
    } catch (error) {
      setFormError(messageFrom(error, 'Could not save the workflow draft.'));
      setReloadKey((current) => current + 1);
    } finally {
      setWorking('');
    }
  };

  const toggleWorkflowStatus = async (workflow: CallWorkflow) => {
    const nextActive = !workflow.is_active;
    if (nextActive && !window.confirm(
      `Mark ${workflow.name} active? This only approves its configuration; the workflow runtime is not connected yet.`,
    )) return;

    setWorking(`${nextActive ? 'activate' : 'deactivate'}-${workflow.id}`);
    setNotice(null);
    try {
      const updated = await api.updateWorkflow(workflow.id, { is_active: nextActive });
      setWorkflows((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice({
        type: 'success',
        text: nextActive
          ? `${updated.name} is marked active for configuration governance. It is not executing calls.`
          : `${updated.name} is now a draft and can be edited.`,
      });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not update workflow status.') });
    } finally {
      setWorking('');
    }
  };

  const deleteWorkflow = async (workflow: CallWorkflow) => {
    if (workflow.is_active) {
      setNotice({ type: 'error', text: `Deactivate ${workflow.name} before deleting it.` });
      return;
    }
    if (!window.confirm(`Delete the ${workflow.name} configuration draft? This cannot be undone.`)) return;

    setWorking(`delete-${workflow.id}`);
    setNotice(null);
    try {
      await api.deleteWorkflow(workflow.id);
      setWorkflows((current) => current.filter((item) => item.id !== workflow.id));
      if (editingId === workflow.id) closeForm();
      setNotice({ type: 'success', text: `${workflow.name} was deleted.` });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not delete the workflow.') });
    } finally {
      setWorking('');
    }
  };

  const selectedTemplate = TEMPLATES.find((template) => template.id === form.template) ?? TEMPLATES[0];
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Design & govern</span>
          <h1>Call workflows</h1>
          <p className="page-subtitle">
            Author validated call-flow configurations from safe templates and control their approval status.
          </p>
        </div>
        <button className="btn btn-primary" onClick={showForm ? closeForm : openCreate}>
          {showForm ? <X size={14} /> : <Plus size={14} />}
          {showForm ? 'Close' : 'Create workflow'}
        </button>
      </div>

      <div className="workflow-runtime-note" role="note">
        <CircleAlert size={17} />
        <div>
          <strong>Configuration control plane</strong>
          <p>
            The visual runtime and simulator are not connected in this release. Saving or marking a workflow active does not route calls,
            launch campaigns, invoke transfers, or fire workflow-node webhooks.
          </p>
        </div>
      </div>

      {notice && (
        <div
          className={`provider-alert workflow-notice ${notice.type === 'error' ? 'provider-alert-error' : ''}`}
          role={notice.type === 'error' ? 'alert' : 'status'}
        >
          {notice.type === 'success' ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
          <span>{notice.text}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setNotice(null)} aria-label="Dismiss message"><X size={13} /></button>
        </div>
      )}

      {showForm && (
        <section className="card workflow-form-panel" aria-labelledby="workflow-form-title">
          <div className="workflow-form-heading">
            <span className="workflow-heading-icon"><GitBranch size={18} /></span>
            <div>
              <span className="page-kicker">Draft authoring</span>
              <h2 id="workflow-form-title">{editingId ? 'Edit workflow draft' : 'Create workflow draft'}</h2>
              <p>Choose a governed linear template. You can edit this draft later.</p>
            </div>
          </div>

          {formError && <div className="auth-error workflow-form-error" role="alert">{formError}</div>}

          <form onSubmit={saveWorkflow}>
            <fieldset className="workflow-fieldset" disabled={Boolean(working)}>
              <fieldset className="workflow-template-selector">
                <legend>Start from a template</legend>
                <div className="workflow-template-grid">
                  {TEMPLATES.map((template) => (
                    <button
                      type="button"
                      className={`workflow-template-card ${form.template === template.id ? 'selected' : ''}`}
                      aria-pressed={form.template === template.id}
                      onClick={() => setForm({ ...form, template: template.id })}
                      key={template.id}
                    >
                      <span className="workflow-template-icon">
                        {template.id === 'assistant' ? <Bot size={17} /> : <PhoneForwarded size={17} />}
                      </span>
                      <strong>{template.name}</strong>
                      <small>{template.description}</small>
                    </button>
                  ))}
                </div>
              </fieldset>

              <div className="form-grid workflow-form-grid">
                <div className="form-group">
                  <label htmlFor="workflow-name">Workflow name</label>
                  <input
                    id="workflow-name"
                    required
                    minLength={2}
                    maxLength={255}
                    value={form.name}
                    placeholder="Inbound support triage"
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="workflow-trigger">Trigger configuration</label>
                  <select
                    id="workflow-trigger"
                    value={form.trigger}
                    onChange={(event) => setForm({ ...form, trigger: event.target.value as WorkflowTrigger })}
                  >
                    {Object.entries(TRIGGER_LABELS).map(([value, label]) => (
                      <option value={value} key={value}>{label}</option>
                    ))}
                  </select>
                  <p className="form-hint">Stored for future runtime routing; it does not listen for triggers yet.</p>
                </div>
                <div className="form-group">
                  <label htmlFor="workflow-agent">Voice agent</label>
                  <select
                    id="workflow-agent"
                    required
                    value={form.agentId}
                    onChange={(event) => setForm({ ...form, agentId: event.target.value })}
                  >
                    <option value="">Choose an agent</option>
                    {agents.map((agent) => (
                      <option value={agent.id} key={agent.id}>
                        {agent.name}{agent.is_active ? '' : ' · inactive'}
                      </option>
                    ))}
                  </select>
                  {agents.length === 0 && (
                    <p className="form-hint">No agents are available. <Link href="/agents">Create an agent first</Link>.</p>
                  )}
                </div>
                <div className="form-group">
                  <label htmlFor="workflow-greeting">Greeting</label>
                  <input
                    id="workflow-greeting"
                    required
                    maxLength={500}
                    value={form.greeting}
                    onChange={(event) => setForm({ ...form, greeting: event.target.value })}
                  />
                </div>
                <div className="form-group workflow-span-full">
                  <label htmlFor="workflow-description">Operator notes <span>{form.description.length}/4000</span></label>
                  <textarea
                    id="workflow-description"
                    maxLength={4000}
                    value={form.description}
                    placeholder="Purpose, approval context, escalation policy, and test expectations…"
                    onChange={(event) => setForm({ ...form, description: event.target.value })}
                  />
                </div>
                {form.template === 'handoff' && (
                  <div className="form-group workflow-span-full">
                    <label htmlFor="workflow-transfer-number">Verified transfer number</label>
                    <input
                      id="workflow-transfer-number"
                      required
                      type="tel"
                      inputMode="tel"
                      autoComplete="tel"
                      value={form.transferNumber}
                      placeholder="+971501234567"
                      onChange={(event) => setForm({ ...form, transferNumber: event.target.value })}
                    />
                    <p className="form-hint">Use E.164 format. No transfer is attempted while the runtime is offline.</p>
                  </div>
                )}
              </div>

              <div className="workflow-sequence-preview" aria-label="Workflow node sequence">
                <div>
                  <span className="page-kicker">Sequence preview</span>
                  <p>Server-generated IDs connect these nodes after the draft is created.</p>
                </div>
                <ol>
                  {selectedTemplate.nodeTypes.map((nodeType, index) => (
                    <li key={`${nodeType}-${index}`}>
                      <span>{nodeIcon(nodeType)}</span>
                      <strong>{NODE_LABELS[nodeType]}</strong>
                    </li>
                  ))}
                </ol>
              </div>
            </fieldset>

            <div className="workflow-form-actions">
              <button type="button" className="btn btn-secondary" onClick={closeForm} disabled={Boolean(working)}>Cancel</button>
              <button type="submit" className="btn btn-primary" disabled={Boolean(working) || agents.length === 0}>
                {working ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
                {working ? 'Saving draft…' : 'Save configuration draft'}
              </button>
            </div>
          </form>
        </section>
      )}

      {loading ? (
        <div className="page-loading" role="status"><Loader2 className="spin" size={16} /> Loading workflow drafts…</div>
      ) : loadError ? (
        <div className="card settings-error" role="alert">
          <div>
            <strong>Workflows are unavailable</strong>
            <p>{loadError}</p>
          </div>
          <button className="btn btn-secondary" onClick={retryLoad}><RotateCw size={13} /> Retry</button>
        </div>
      ) : workflows.length === 0 ? (
        <div className="empty-state workflow-empty">
          <span className="empty-state-icon"><GitBranch size={22} /></span>
          <h3>No workflow drafts</h3>
          <p>Create a safe linear configuration now, then test it when the simulator and runtime are connected.</p>
          <button className="btn btn-primary" onClick={openCreate}><Plus size={14} /> Create workflow</button>
        </div>
      ) : (
        <section className="workflow-list" aria-label="Workflow configurations">
          {workflows.map((workflow) => {
            const orderedNodes = [...workflow.nodes].sort((left, right) => left.position - right.position);
            const isWorking = working.endsWith(workflow.id);
            return (
              <article className="workflow-card" key={workflow.id}>
                <div className="workflow-card-header">
                  <span className="workflow-card-icon"><GitBranch size={17} /></span>
                  <div>
                    <div className="workflow-title-row">
                      <h2>{workflow.name}</h2>
                      <span className={`badge ${workflow.is_active ? 'badge-warning' : 'badge-neutral'}`}>
                        {workflow.is_active ? 'Active config' : 'Draft'}
                      </span>
                    </div>
                    <p>{workflow.description || 'No operator notes.'}</p>
                  </div>
                </div>

                <dl className="workflow-metadata">
                  <div><dt>Trigger</dt><dd>{TRIGGER_LABELS[workflow.trigger_type]}</dd></div>
                  <div><dt>Agent</dt><dd>{workflow.agent_id ? agentNames.get(workflow.agent_id) ?? 'Unavailable agent' : 'Not assigned'}</dd></div>
                  <div><dt>Nodes</dt><dd>{orderedNodes.length}</dd></div>
                </dl>

                <ol className="workflow-card-sequence" aria-label={`${workflow.name} node sequence`}>
                  {orderedNodes.map((node) => (
                    <li key={node.id}>
                      <span>{nodeIcon(node.node_type)}</span>
                      {NODE_LABELS[node.node_type]}
                    </li>
                  ))}
                </ol>

                <div className="workflow-card-footer">
                  <p>{workflow.is_active ? 'Configuration approved · runtime offline' : 'Editable configuration draft'}</p>
                  <div className="workflow-card-actions">
                    <button className="btn btn-secondary btn-sm" onClick={() => editWorkflow(workflow)} disabled={Boolean(working)}>
                      {working === `deactivate-${workflow.id}` ? <Loader2 className="spin" size={12} /> : <Pencil size={12} />}
                      Edit
                    </button>
                    <button className="btn btn-secondary btn-sm" onClick={() => toggleWorkflowStatus(workflow)} disabled={Boolean(working)}>
                      {isWorking ? <Loader2 className="spin" size={12} /> : <CheckCircle2 size={12} />}
                      {workflow.is_active ? 'Deactivate' : 'Mark active'}
                    </button>
                    <button className="btn btn-danger btn-sm" onClick={() => deleteWorkflow(workflow)} disabled={Boolean(working)}>
                      {working === `delete-${workflow.id}` ? <Loader2 className="spin" size={12} /> : <Trash2 size={12} />}
                      Delete
                    </button>
                  </div>
                </div>
              </article>
            );
          })}
        </section>
      )}
    </Layout>
  );
}
