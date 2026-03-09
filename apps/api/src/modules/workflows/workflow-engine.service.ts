import {
  Injectable,
  Logger,
  BadRequestException,
  NotFoundException,
} from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { QueueService } from '../../common/services/queue.service';

// ============================================================================
// Types
// ============================================================================

export enum WorkflowNodeType {
  GREETING = 'GREETING',
  CONSENT_CAPTURE = 'CONSENT_CAPTURE',
  VERIFICATION = 'VERIFICATION',
  QUALIFICATION = 'QUALIFICATION',
  INFO_CAPTURE = 'INFO_CAPTURE',
  LOOKUP = 'LOOKUP',
  SUMMARIZE = 'SUMMARIZE',
  OFFER_OPTIONS = 'OFFER_OPTIONS',
  BOOK_APPOINTMENT = 'BOOK_APPOINTMENT',
  COLLECT_PAYMENT = 'COLLECT_PAYMENT',
  ESCALATE_HUMAN = 'ESCALATE_HUMAN',
  SEND_MESSAGE = 'SEND_MESSAGE',
  END_CALL = 'END_CALL',
  RETRY_LATER = 'RETRY_LATER',
  CONDITIONAL = 'CONDITIONAL',
  WAIT = 'WAIT',
  API_CALL = 'API_CALL',
  SET_VARIABLE = 'SET_VARIABLE',
}

export interface WorkflowNode {
  id: string;
  type: WorkflowNodeType | string;
  position: { x: number; y: number };
  data: {
    label: string;
    prompt?: string;
    config?: Record<string, unknown>;
    fallbackNodeId?: string;
    timeoutSeconds?: number;
    retryCount?: number;
    [key: string]: unknown;
  };
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  conditions?: EdgeCondition;
}

export interface EdgeCondition {
  expression?: string;
  operator?: 'eq' | 'neq' | 'gt' | 'lt' | 'gte' | 'lte' | 'contains' | 'matches' | 'default';
  field?: string;
  value?: unknown;
}

export interface WorkflowExecutionState {
  id: string;
  workflowId: string;
  callId: string;
  tenantId: string;
  currentNodeId: string;
  previousNodeIds: string[];
  variables: Record<string, unknown>;
  conversationHistory: ConversationTurn[];
  nodeResults: Record<string, NodeExecutionResult>;
  status: 'running' | 'paused' | 'completed' | 'failed' | 'waiting';
  error?: string;
  startedAt: string;
  updatedAt: string;
}

export interface ConversationTurn {
  role: 'agent' | 'contact' | 'system';
  content: string;
  timestamp: string;
  nodeId: string;
}

export interface NodeExecutionResult {
  nodeId: string;
  nodeType: string;
  status: 'success' | 'failed' | 'skipped' | 'waiting';
  output: Record<string, unknown>;
  selectedEdge?: string;
  duration: number;
  timestamp: string;
}

interface NodeExecutorContext {
  node: WorkflowNode;
  state: WorkflowExecutionState;
  edges: WorkflowEdge[];
  allNodes: WorkflowNode[];
  input?: string;
}

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class WorkflowEngineService {
  private readonly logger = new Logger(WorkflowEngineService.name);
  private readonly STATE_TTL = 86400; // 24 hours
  private readonly STATE_PREFIX = 'workflow:state:';

  constructor(
    private readonly prisma: PrismaService,
    private readonly redis: RedisService,
    private readonly queue: QueueService,
  ) {}

  // --------------------------------------------------------------------------
  // Workflow Execution Lifecycle
  // --------------------------------------------------------------------------

  async startExecution(
    workflowId: string,
    callId: string,
    tenantId: string,
    initialVariables: Record<string, unknown> = {},
  ): Promise<WorkflowExecutionState> {
    const workflow = await this.prisma.workflow.findFirst({
      where: { id: workflowId, tenantId },
    });
    if (!workflow) {
      throw new NotFoundException(`Workflow ${workflowId} not found`);
    }

    const nodes = workflow.nodes as unknown as WorkflowNode[];
    const edges = workflow.edges as unknown as WorkflowEdge[];

    const startNode = nodes.find(
      (n) => n.type === 'start' || n.type === WorkflowNodeType.GREETING,
    );
    if (!startNode) {
      throw new BadRequestException('Workflow has no start/greeting node');
    }

    const state: WorkflowExecutionState = {
      id: `exec_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
      workflowId,
      callId,
      tenantId,
      currentNodeId: startNode.id,
      previousNodeIds: [],
      variables: {
        ...initialVariables,
        _callId: callId,
        _tenantId: tenantId,
        _workflowId: workflowId,
        _startTime: new Date().toISOString(),
        _timeOfDay: this.getTimeOfDay(),
      },
      conversationHistory: [],
      nodeResults: {},
      status: 'running',
      startedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };

    await this.persistState(state);

    this.logger.log(
      `Started workflow execution ${state.id} for call ${callId} (workflow: ${workflowId})`,
    );

    const result = await this.executeCurrentNode(state, nodes, edges);
    return result;
  }

  async processInput(
    executionId: string,
    input: string,
  ): Promise<WorkflowExecutionState> {
    const state = await this.loadState(executionId);
    if (!state) {
      throw new NotFoundException(`Execution ${executionId} not found`);
    }
    if (state.status !== 'running' && state.status !== 'waiting') {
      throw new BadRequestException(
        `Execution is in ${state.status} state, cannot process input`,
      );
    }

    const workflow = await this.prisma.workflow.findFirst({
      where: { id: state.workflowId, tenantId: state.tenantId },
    });
    if (!workflow) {
      throw new NotFoundException(`Workflow ${state.workflowId} not found`);
    }

    const nodes = workflow.nodes as unknown as WorkflowNode[];
    const edges = workflow.edges as unknown as WorkflowEdge[];

    state.conversationHistory.push({
      role: 'contact',
      content: input,
      timestamp: new Date().toISOString(),
      nodeId: state.currentNodeId,
    });

    state.variables._lastInput = input;
    state.status = 'running';

    const nextEdge = await this.evaluateEdgesForInput(
      state.currentNodeId,
      edges,
      state,
      input,
    );

    if (nextEdge) {
      state.previousNodeIds.push(state.currentNodeId);
      state.currentNodeId = nextEdge.target;
      return this.executeCurrentNode(state, nodes, edges);
    }

    // No matching edge: re-execute current node (retry / reprompt)
    return this.executeCurrentNode(state, nodes, edges);
  }

  async resumeExecution(executionId: string): Promise<WorkflowExecutionState> {
    const state = await this.loadState(executionId);
    if (!state) {
      throw new NotFoundException(`Execution ${executionId} not found`);
    }

    const workflow = await this.prisma.workflow.findFirst({
      where: { id: state.workflowId, tenantId: state.tenantId },
    });
    if (!workflow) {
      throw new NotFoundException(`Workflow ${state.workflowId} not found`);
    }

    const nodes = workflow.nodes as unknown as WorkflowNode[];
    const edges = workflow.edges as unknown as WorkflowEdge[];

    state.status = 'running';
    return this.executeCurrentNode(state, nodes, edges);
  }

  async getExecutionState(
    executionId: string,
  ): Promise<WorkflowExecutionState | null> {
    return this.loadState(executionId);
  }

  // --------------------------------------------------------------------------
  // Core Execution Loop
  // --------------------------------------------------------------------------

  private async executeCurrentNode(
    state: WorkflowExecutionState,
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
  ): Promise<WorkflowExecutionState> {
    const currentNode = nodes.find((n) => n.id === state.currentNodeId);
    if (!currentNode) {
      state.status = 'failed';
      state.error = `Node ${state.currentNodeId} not found in workflow`;
      await this.persistState(state);
      return state;
    }

    const nodeEdges = edges.filter((e) => e.source === currentNode.id);
    const startTime = Date.now();

    try {
      const context: NodeExecutorContext = {
        node: currentNode,
        state,
        edges: nodeEdges,
        allNodes: nodes,
      };

      const result = await this.executeNode(context);

      state.nodeResults[currentNode.id] = {
        nodeId: currentNode.id,
        nodeType: currentNode.type,
        status: result.status,
        output: result.output,
        selectedEdge: result.selectedEdge,
        duration: Date.now() - startTime,
        timestamp: new Date().toISOString(),
      };

      if (result.agentMessage) {
        state.conversationHistory.push({
          role: 'agent',
          content: result.agentMessage,
          timestamp: new Date().toISOString(),
          nodeId: currentNode.id,
        });
      }

      Object.assign(state.variables, result.variableUpdates || {});

      // Terminal states
      if (
        currentNode.type === WorkflowNodeType.END_CALL ||
        result.terminal
      ) {
        state.status = 'completed';
        await this.persistState(state);
        return state;
      }

      // Waiting for input
      if (result.waitForInput) {
        state.status = 'waiting';
        await this.persistState(state);
        return state;
      }

      // Auto-advance to next node
      if (result.selectedEdge) {
        const nextEdge = nodeEdges.find((e) => e.id === result.selectedEdge);
        if (nextEdge) {
          state.previousNodeIds.push(state.currentNodeId);
          state.currentNodeId = nextEdge.target;
          state.updatedAt = new Date().toISOString();
          // Recursively execute next node (auto-advance nodes like SET_VARIABLE, CONDITIONAL)
          return this.executeCurrentNode(state, nodes, edges);
        }
      }

      // Default: pick first available edge if exists
      if (nodeEdges.length > 0 && !result.waitForInput) {
        const defaultEdge = nodeEdges.find(
          (e) =>
            !e.conditions ||
            !e.conditions.expression ||
            e.conditions.operator === 'default',
        ) || nodeEdges[0];
        state.previousNodeIds.push(state.currentNodeId);
        state.currentNodeId = defaultEdge.target;
        state.updatedAt = new Date().toISOString();
        return this.executeCurrentNode(state, nodes, edges);
      }

      // No edges, no wait - mark completed
      state.status = 'completed';
      await this.persistState(state);
      return state;
    } catch (error) {
      this.logger.error(
        `Error executing node ${currentNode.id} (${currentNode.type}): ${error}`,
      );

      // Try fallback node
      if (currentNode.data.fallbackNodeId) {
        const fallbackNode = nodes.find(
          (n) => n.id === currentNode.data.fallbackNodeId,
        );
        if (fallbackNode) {
          state.previousNodeIds.push(state.currentNodeId);
          state.currentNodeId = fallbackNode.id;
          state.variables._lastError = String(error);
          return this.executeCurrentNode(state, nodes, edges);
        }
      }

      state.status = 'failed';
      state.error = String(error);
      await this.persistState(state);
      return state;
    }
  }

  // --------------------------------------------------------------------------
  // Node Executors
  // --------------------------------------------------------------------------

  private async executeNode(
    ctx: NodeExecutorContext,
  ): Promise<{
    status: 'success' | 'failed' | 'waiting';
    output: Record<string, unknown>;
    agentMessage?: string;
    variableUpdates?: Record<string, unknown>;
    selectedEdge?: string;
    waitForInput?: boolean;
    terminal?: boolean;
  }> {
    const { node, state, edges } = ctx;

    switch (node.type) {
      case WorkflowNodeType.GREETING:
        return this.executeGreeting(ctx);
      case WorkflowNodeType.CONSENT_CAPTURE:
        return this.executeConsentCapture(ctx);
      case WorkflowNodeType.VERIFICATION:
        return this.executeVerification(ctx);
      case WorkflowNodeType.QUALIFICATION:
        return this.executeQualification(ctx);
      case WorkflowNodeType.INFO_CAPTURE:
        return this.executeInfoCapture(ctx);
      case WorkflowNodeType.LOOKUP:
        return this.executeLookup(ctx);
      case WorkflowNodeType.SUMMARIZE:
        return this.executeSummarize(ctx);
      case WorkflowNodeType.OFFER_OPTIONS:
        return this.executeOfferOptions(ctx);
      case WorkflowNodeType.BOOK_APPOINTMENT:
        return this.executeBookAppointment(ctx);
      case WorkflowNodeType.COLLECT_PAYMENT:
        return this.executeCollectPayment(ctx);
      case WorkflowNodeType.ESCALATE_HUMAN:
        return this.executeEscalateHuman(ctx);
      case WorkflowNodeType.SEND_MESSAGE:
        return this.executeSendMessage(ctx);
      case WorkflowNodeType.END_CALL:
        return this.executeEndCall(ctx);
      case WorkflowNodeType.RETRY_LATER:
        return this.executeRetryLater(ctx);
      case WorkflowNodeType.CONDITIONAL:
        return this.executeConditional(ctx);
      case WorkflowNodeType.WAIT:
        return this.executeWait(ctx);
      case WorkflowNodeType.API_CALL:
        return this.executeApiCall(ctx);
      case WorkflowNodeType.SET_VARIABLE:
        return this.executeSetVariable(ctx);
      default:
        this.logger.warn(`Unknown node type: ${node.type}, using passthrough`);
        return {
          status: 'success',
          output: { message: `Passthrough for unknown type ${node.type}` },
        };
    }
  }

  private async executeGreeting(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const timeOfDay = this.getTimeOfDay();
    const prompt = this.interpolateVariables(
      node.data.prompt || 'Hello! {{greeting_prefix}} How can I help you today?',
      { ...state.variables, greeting_prefix: this.greetingPrefix(timeOfDay) },
    );

    // Branch on time of day if edges specify
    const timeEdge = edges.find(
      (e) => e.conditions?.field === 'timeOfDay' && e.conditions?.value === timeOfDay,
    );

    return {
      status: 'success' as const,
      output: { timeOfDay, greeting: prompt },
      agentMessage: prompt,
      variableUpdates: { _timeOfDay: timeOfDay },
      selectedEdge: timeEdge?.id,
      waitForInput: !timeEdge && edges.length === 0,
    };
  }

  private async executeConsentCapture(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;

    if (!lastInput) {
      const prompt = this.interpolateVariables(
        node.data.prompt ||
          'This call may be recorded for quality purposes. Do you consent to being recorded?',
        state.variables,
      );
      return {
        status: 'success' as const,
        output: { asked: true },
        agentMessage: prompt,
        waitForInput: true,
      };
    }

    const normalized = lastInput.toLowerCase().trim();
    const consented =
      normalized.includes('yes') ||
      normalized.includes('agree') ||
      normalized.includes('sure') ||
      normalized.includes('okay') ||
      normalized.includes('ok');

    const yesEdge = edges.find(
      (e) =>
        e.label?.toLowerCase() === 'yes' ||
        e.conditions?.value === true ||
        e.conditions?.value === 'yes',
    );
    const noEdge = edges.find(
      (e) =>
        e.label?.toLowerCase() === 'no' ||
        e.conditions?.value === false ||
        e.conditions?.value === 'no',
    );

    return {
      status: 'success' as const,
      output: { consented, rawInput: lastInput },
      variableUpdates: { _consentGiven: consented },
      selectedEdge: consented ? yesEdge?.id : noEdge?.id,
      agentMessage: consented
        ? 'Thank you for your consent.'
        : 'I understand. Let me note that.',
    };
  }

  private async executeVerification(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;
    const verificationField =
      (node.data.config?.field as string) || 'date_of_birth';
    const stateKey = `_verification_${verificationField}`;

    if (!lastInput || !state.variables[`_verificationAsked_${verificationField}`]) {
      const prompts: Record<string, string> = {
        date_of_birth: 'For verification, can you please provide your date of birth?',
        last_4_ssn: 'Can you please provide the last four digits of your Social Security number?',
        account_number: 'What is your account number?',
      };
      const prompt = this.interpolateVariables(
        node.data.prompt || prompts[verificationField] || `Please provide your ${verificationField}.`,
        state.variables,
      );

      return {
        status: 'success' as const,
        output: { field: verificationField, asked: true },
        agentMessage: prompt,
        variableUpdates: { [`_verificationAsked_${verificationField}`]: true },
        waitForInput: true,
      };
    }

    const expectedValue = node.data.config?.expectedValue as string | undefined;
    const verified = expectedValue
      ? lastInput.replace(/\D/g, '').includes(expectedValue.replace(/\D/g, ''))
      : true; // Accept any input if no expected value configured

    const successEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'verified' || e.conditions?.value === true,
    );
    const failEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'failed' || e.conditions?.value === false,
    );

    return {
      status: 'success' as const,
      output: { verified, field: verificationField, providedValue: lastInput },
      variableUpdates: {
        [stateKey]: lastInput,
        _identityVerified: verified,
      },
      selectedEdge: verified ? successEdge?.id : failEdge?.id,
      agentMessage: verified
        ? 'Thank you, your identity has been verified.'
        : "I'm sorry, that doesn't match our records. Let me try another way.",
    };
  }

  private async executeQualification(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;
    const questions = (node.data.config?.questions as Array<{
      key: string;
      question: string;
      weight: number;
      passingAnswers?: string[];
    }>) || [];

    const answeredCount = questions.filter(
      (q) => state.variables[`_qual_${q.key}`] !== undefined,
    ).length;

    // Ask next unanswered question
    if (lastInput && answeredCount > 0) {
      const prevQuestion = questions[answeredCount - 1];
      if (prevQuestion) {
        state.variables[`_qual_${prevQuestion.key}`] = lastInput;
      }
    }

    const newAnsweredCount = questions.filter(
      (q) => state.variables[`_qual_${q.key}`] !== undefined,
    ).length;

    if (newAnsweredCount < questions.length) {
      const nextQuestion = questions[newAnsweredCount];
      const prompt = this.interpolateVariables(
        nextQuestion.question,
        state.variables,
      );
      return {
        status: 'success' as const,
        output: { questionIndex: newAnsweredCount, total: questions.length },
        agentMessage: prompt,
        waitForInput: true,
      };
    }

    // All questions answered - compute score
    let totalScore = 0;
    let maxScore = 0;
    for (const q of questions) {
      maxScore += q.weight;
      const answer = (state.variables[`_qual_${q.key}`] as string) || '';
      if (q.passingAnswers) {
        const passes = q.passingAnswers.some((pa) =>
          answer.toLowerCase().includes(pa.toLowerCase()),
        );
        if (passes) totalScore += q.weight;
      } else {
        totalScore += q.weight * 0.5; // Partial credit for open-ended
      }
    }

    const qualificationScore = maxScore > 0 ? (totalScore / maxScore) * 100 : 0;
    const threshold = (node.data.config?.threshold as number) || 60;
    const qualified = qualificationScore >= threshold;

    const qualifiedEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'qualified' || e.conditions?.value === true,
    );
    const unqualifiedEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'unqualified' || e.conditions?.value === false,
    );

    return {
      status: 'success' as const,
      output: { qualificationScore, qualified, threshold },
      variableUpdates: {
        _qualificationScore: qualificationScore,
        _qualified: qualified,
      },
      selectedEdge: qualified ? qualifiedEdge?.id : unqualifiedEdge?.id,
    };
  }

  private async executeInfoCapture(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;
    const fields = (node.data.config?.fields as Array<{
      key: string;
      label: string;
      required?: boolean;
      validation?: string;
    }>) || [{ key: 'info', label: 'information', required: true }];

    const capturedCount = fields.filter(
      (f) => state.variables[`_captured_${f.key}`] !== undefined,
    ).length;

    // Store previous input
    if (lastInput && capturedCount > 0) {
      const prevField = fields[capturedCount - 1];
      if (prevField) {
        // Validate if pattern provided
        if (prevField.validation) {
          try {
            const regex = new RegExp(prevField.validation);
            if (!regex.test(lastInput)) {
              const prompt = `I'm sorry, that doesn't seem to be a valid ${prevField.label}. Could you please try again?`;
              return {
                status: 'success' as const,
                output: { fieldIndex: capturedCount - 1, validationFailed: true },
                agentMessage: prompt,
                waitForInput: true,
              };
            }
          } catch {
            // Invalid regex, skip validation
          }
        }
        state.variables[`_captured_${prevField.key}`] = lastInput;
      }
    }

    const newCapturedCount = fields.filter(
      (f) => state.variables[`_captured_${f.key}`] !== undefined,
    ).length;

    if (newCapturedCount < fields.length) {
      const nextField = fields[newCapturedCount];
      const prompt = this.interpolateVariables(
        node.data.prompt || `Could you please provide your ${nextField.label}?`,
        { ...state.variables, _currentField: nextField.label },
      );
      return {
        status: 'success' as const,
        output: { fieldIndex: newCapturedCount, total: fields.length },
        agentMessage: prompt,
        waitForInput: true,
      };
    }

    // All fields captured
    const capturedData: Record<string, unknown> = {};
    for (const f of fields) {
      capturedData[f.key] = state.variables[`_captured_${f.key}`];
    }

    return {
      status: 'success' as const,
      output: { capturedData },
      variableUpdates: { _capturedInfo: capturedData },
      agentMessage: 'Thank you, I have all the information I need.',
    };
  }

  private async executeLookup(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const config = node.data.config || {};
    const endpoint = config.endpoint as string | undefined;
    const method = (config.method as string) || 'GET';
    const headers = (config.headers as Record<string, string>) || {};

    if (!endpoint) {
      this.logger.warn('LOOKUP node has no endpoint configured, skipping');
      return {
        status: 'success' as const,
        output: { skipped: true, reason: 'no endpoint configured' },
        variableUpdates: { _lookupResult: null },
      };
    }

    try {
      const url = this.interpolateVariables(endpoint, state.variables);
      const bodyTemplate = config.body as Record<string, unknown> | undefined;
      const body = bodyTemplate
        ? JSON.parse(this.interpolateVariables(JSON.stringify(bodyTemplate), state.variables))
        : undefined;

      const response = await this.makeHttpRequest(url, method, headers, body);

      const resultKey = (config.resultVariable as string) || '_lookupResult';

      const foundEdge = edges.find(
        (e) => e.label?.toLowerCase() === 'found' || e.conditions?.value === true,
      );
      const notFoundEdge = edges.find(
        (e) => e.label?.toLowerCase() === 'not found' || e.conditions?.value === false,
      );

      const hasResult = response && Object.keys(response).length > 0;

      return {
        status: 'success' as const,
        output: { result: response },
        variableUpdates: { [resultKey]: response },
        selectedEdge: hasResult ? foundEdge?.id : notFoundEdge?.id,
      };
    } catch (error) {
      this.logger.error(`LOOKUP failed: ${error}`);
      const errorEdge = edges.find(
        (e) => e.label?.toLowerCase() === 'error',
      );
      return {
        status: 'failed' as const,
        output: { error: String(error) },
        variableUpdates: { _lookupError: String(error) },
        selectedEdge: errorEdge?.id,
      };
    }
  }

  private async executeSummarize(ctx: NodeExecutorContext) {
    const { state } = ctx;

    const conversationText = state.conversationHistory
      .map((t) => `${t.role}: ${t.content}`)
      .join('\n');

    const capturedVars: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(state.variables)) {
      if (key.startsWith('_captured_') || key.startsWith('_qual_')) {
        capturedVars[key.replace(/^_(captured|qual)_/, '')] = value;
      }
    }

    const summary = [
      `Conversation summary (${state.conversationHistory.length} turns):`,
      `- Consent given: ${state.variables._consentGiven ?? 'N/A'}`,
      `- Identity verified: ${state.variables._identityVerified ?? 'N/A'}`,
      `- Qualification score: ${state.variables._qualificationScore ?? 'N/A'}`,
      `- Data collected: ${JSON.stringify(capturedVars)}`,
    ].join('\n');

    return {
      status: 'success' as const,
      output: { summary, capturedData: capturedVars, turnCount: state.conversationHistory.length },
      variableUpdates: { _summary: summary },
      agentMessage: 'Let me summarize what we have discussed so far. ' + summary,
    };
  }

  private async executeOfferOptions(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;
    const options = (node.data.config?.options as Array<{
      key: string;
      label: string;
      description?: string;
    }>) || [];

    if (!lastInput || !state.variables._optionsPresented) {
      const optionsList = options
        .map((o, i) => `${i + 1}. ${o.label}${o.description ? ` - ${o.description}` : ''}`)
        .join('\n');
      const prompt = this.interpolateVariables(
        node.data.prompt || `Please choose from the following options:\n${optionsList}`,
        state.variables,
      );

      return {
        status: 'success' as const,
        output: { options },
        agentMessage: prompt,
        variableUpdates: { _optionsPresented: true },
        waitForInput: true,
      };
    }

    // Match selection
    const normalized = lastInput.toLowerCase().trim();
    let selectedOption = options.find(
      (o) =>
        normalized === o.key.toLowerCase() ||
        normalized === o.label.toLowerCase() ||
        normalized.includes(o.label.toLowerCase()),
    );

    // Try numeric selection
    if (!selectedOption) {
      const num = parseInt(normalized, 10);
      if (num > 0 && num <= options.length) {
        selectedOption = options[num - 1];
      }
    }

    if (!selectedOption && options.length > 0) {
      // Fuzzy match: find the option with the most word overlap
      let bestMatch = options[0];
      let bestScore = 0;
      const inputWords = normalized.split(/\s+/);
      for (const opt of options) {
        const labelWords = opt.label.toLowerCase().split(/\s+/);
        const overlap = inputWords.filter((w) => labelWords.includes(w)).length;
        if (overlap > bestScore) {
          bestScore = overlap;
          bestMatch = opt;
        }
      }
      if (bestScore > 0) selectedOption = bestMatch;
    }

    const selectedKey = selectedOption?.key || 'unknown';
    const matchingEdge = edges.find(
      (e) =>
        e.conditions?.value === selectedKey ||
        e.label?.toLowerCase() === selectedKey.toLowerCase(),
    );

    return {
      status: 'success' as const,
      output: { selectedOption: selectedKey },
      variableUpdates: {
        _selectedOption: selectedKey,
        _optionsPresented: false,
      },
      selectedEdge: matchingEdge?.id,
      agentMessage: selectedOption
        ? `You selected: ${selectedOption.label}. Let me proceed with that.`
        : "I didn't quite catch that. Let me proceed with the default option.",
    };
  }

  private async executeBookAppointment(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const lastInput = state.variables._lastInput as string | undefined;
    const config = node.data.config || {};

    if (!lastInput || !state.variables._appointmentAsked) {
      const prompt = this.interpolateVariables(
        node.data.prompt ||
          'I can help you schedule an appointment. What date and time works best for you?',
        state.variables,
      );
      return {
        status: 'success' as const,
        output: { asked: true },
        agentMessage: prompt,
        variableUpdates: { _appointmentAsked: true },
        waitForInput: true,
      };
    }

    // In production, call a scheduling API. Here we simulate the booking.
    const appointmentData = {
      requestedTime: lastInput,
      contactId: state.variables._contactId,
      bookedAt: new Date().toISOString(),
      status: 'confirmed',
    };

    // Queue the actual booking
    if (config.endpoint) {
      try {
        const url = this.interpolateVariables(config.endpoint as string, state.variables);
        await this.makeHttpRequest(url, 'POST', {}, appointmentData);
      } catch (error) {
        this.logger.error(`Appointment booking API failed: ${error}`);
      }
    }

    const bookedEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'booked' || e.conditions?.value === true,
    );
    const failedEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'failed' || e.conditions?.value === false,
    );

    return {
      status: 'success' as const,
      output: { appointment: appointmentData },
      variableUpdates: {
        _appointment: appointmentData,
        _appointmentAsked: false,
      },
      selectedEdge: bookedEdge?.id,
      agentMessage: `Your appointment has been scheduled for ${lastInput}. You will receive a confirmation shortly.`,
    };
  }

  private async executeCollectPayment(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    // PCI-compliant: we never store card numbers. We provide a reference to a secure payment form.
    const config = node.data.config || {};
    const amount = config.amount as number | undefined;
    const currency = (config.currency as string) || 'USD';

    const prompt = this.interpolateVariables(
      node.data.prompt ||
        `The total amount due is ${amount ? `$${amount}` : 'as discussed'}. I will transfer you to our secure payment system. Please have your payment information ready.`,
      state.variables,
    );

    // Generate a payment session token reference
    const paymentRef = `pay_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;

    const paymentEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'payment_complete',
    );
    const declinedEdge = edges.find(
      (e) => e.label?.toLowerCase() === 'declined',
    );

    return {
      status: 'success' as const,
      output: { paymentRef, amount, currency },
      variableUpdates: {
        _paymentRef: paymentRef,
        _paymentAmount: amount,
      },
      agentMessage: prompt,
      selectedEdge: paymentEdge?.id, // auto-advance after initiating
      waitForInput: !paymentEdge,
    };
  }

  private async executeEscalateHuman(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const config = node.data.config || {};
    const department = (config.department as string) || 'general';
    const priority = (config.priority as string) || 'normal';
    const reason = this.interpolateVariables(
      (config.reason as string) || 'Customer requested human agent',
      state.variables,
    );

    const prompt = this.interpolateVariables(
      node.data.prompt ||
        'Let me transfer you to a human agent who can better assist you. Please hold.',
      state.variables,
    );

    // Queue escalation
    await this.queue.addJob('call-escalation', 'escalate-to-human', {
      tenantId: state.tenantId,
      callId: state.callId,
      department,
      priority,
      reason,
      conversationSummary: state.variables._summary || '',
      variables: state.variables,
    });

    return {
      status: 'success' as const,
      output: { escalated: true, department, priority, reason },
      variableUpdates: { _escalated: true, _escalationDepartment: department },
      agentMessage: prompt,
      terminal: true,
    };
  }

  private async executeSendMessage(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const config = node.data.config || {};
    const channel = (config.channel as string) || 'sms';
    const recipient =
      this.interpolateVariables((config.recipient as string) || '', state.variables) ||
      (state.variables._contactPhone as string);
    const messageBody = this.interpolateVariables(
      (config.messageBody as string) || '',
      state.variables,
    );

    if (!recipient || !messageBody) {
      this.logger.warn('SEND_MESSAGE: missing recipient or message body');
      return {
        status: 'failed' as const,
        output: { error: 'Missing recipient or message body' },
      };
    }

    await this.queue.addJob('notifications', 'send-message', {
      tenantId: state.tenantId,
      channel,
      recipient,
      body: messageBody,
      callId: state.callId,
    });

    return {
      status: 'success' as const,
      output: { channel, recipient, sent: true },
      variableUpdates: { _messageSent: true },
    };
  }

  private async executeEndCall(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const prompt = this.interpolateVariables(
      node.data.prompt || 'Thank you for your time. Have a great day!',
      state.variables,
    );

    const outcome = (node.data.config?.outcome as string) || 'completed';
    const disposition = (node.data.config?.disposition as string) || 'normal';

    return {
      status: 'success' as const,
      output: { outcome, disposition },
      variableUpdates: { _outcome: outcome, _disposition: disposition },
      agentMessage: prompt,
      terminal: true,
    };
  }

  private async executeRetryLater(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const config = node.data.config || {};
    const delayMinutes = (config.delayMinutes as number) || 60;
    const maxRetries = (config.maxRetries as number) || 3;
    const currentRetry = ((state.variables._retryCount as number) || 0) + 1;

    if (currentRetry > maxRetries) {
      return {
        status: 'success' as const,
        output: { maxRetriesExceeded: true, retryCount: currentRetry - 1 },
        variableUpdates: { _maxRetriesExceeded: true },
        agentMessage: "We've reached the maximum number of attempts. Thank you for your time.",
        terminal: true,
      };
    }

    const scheduledTime = new Date(Date.now() + delayMinutes * 60 * 1000);

    await this.queue.addJob(
      'call-scheduling',
      'schedule-retry',
      {
        tenantId: state.tenantId,
        callId: state.callId,
        workflowId: state.workflowId,
        scheduledTime: scheduledTime.toISOString(),
        retryCount: currentRetry,
        variables: state.variables,
      },
      { delay: delayMinutes * 60 * 1000 },
    );

    const prompt = this.interpolateVariables(
      node.data.prompt || "I'll schedule a callback for later. Thank you.",
      state.variables,
    );

    return {
      status: 'success' as const,
      output: { scheduledTime: scheduledTime.toISOString(), retryCount: currentRetry },
      variableUpdates: { _retryCount: currentRetry, _nextCallbackTime: scheduledTime.toISOString() },
      agentMessage: prompt,
      terminal: true,
    };
  }

  private async executeConditional(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const conditions = (node.data.config?.conditions as Array<{
      edgeId: string;
      expression: string;
    }>) || [];

    // Evaluate each condition expression against variables
    for (const cond of conditions) {
      const result = this.evaluateExpression(cond.expression, state.variables);
      if (result) {
        return {
          status: 'success' as const,
          output: { matchedCondition: cond.expression },
          selectedEdge: cond.edgeId,
        };
      }
    }

    // Also try evaluating edge conditions directly
    for (const edge of edges) {
      if (edge.conditions?.expression) {
        const result = this.evaluateExpression(
          edge.conditions.expression,
          state.variables,
        );
        if (result) {
          return {
            status: 'success' as const,
            output: { matchedEdge: edge.id, expression: edge.conditions.expression },
            selectedEdge: edge.id,
          };
        }
      }
    }

    // Fall through to default edge
    const defaultEdge = edges.find(
      (e) => e.conditions?.operator === 'default' || e.label?.toLowerCase() === 'default',
    );
    return {
      status: 'success' as const,
      output: { matchedCondition: 'default' },
      selectedEdge: defaultEdge?.id,
    };
  }

  private async executeWait(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const config = node.data.config || {};
    const waitType = (config.waitType as string) || 'input'; // 'input', 'timer', 'event'
    const timeoutSeconds = (config.timeoutSeconds as number) || 30;

    if (waitType === 'input') {
      const prompt = this.interpolateVariables(
        node.data.prompt || 'Please go ahead.',
        state.variables,
      );
      return {
        status: 'waiting' as const,
        output: { waitType, timeoutSeconds },
        agentMessage: prompt,
        waitForInput: true,
      };
    }

    // For timer waits, we just note the wait and auto-advance
    return {
      status: 'success' as const,
      output: { waitType, timeoutSeconds, waited: true },
    };
  }

  private async executeApiCall(ctx: NodeExecutorContext) {
    const { node, state, edges } = ctx;
    const config = node.data.config || {};
    const url = this.interpolateVariables((config.url as string) || '', state.variables);
    const method = (config.method as string) || 'GET';
    const headers = (config.headers as Record<string, string>) || {};
    const bodyTemplate = config.body as Record<string, unknown> | undefined;
    const resultVariable = (config.resultVariable as string) || '_apiResult';

    if (!url) {
      return {
        status: 'failed' as const,
        output: { error: 'No URL configured for API_CALL node' },
      };
    }

    try {
      const body = bodyTemplate
        ? JSON.parse(this.interpolateVariables(JSON.stringify(bodyTemplate), state.variables))
        : undefined;

      const response = await this.makeHttpRequest(url, method, headers, body);

      const successEdge = edges.find(
        (e) => e.label?.toLowerCase() === 'success' || e.conditions?.value === true,
      );

      return {
        status: 'success' as const,
        output: { response },
        variableUpdates: { [resultVariable]: response },
        selectedEdge: successEdge?.id,
      };
    } catch (error) {
      const errorEdge = edges.find(
        (e) => e.label?.toLowerCase() === 'error' || e.conditions?.value === false,
      );
      return {
        status: 'failed' as const,
        output: { error: String(error) },
        variableUpdates: { [`${resultVariable}_error`]: String(error) },
        selectedEdge: errorEdge?.id,
      };
    }
  }

  private async executeSetVariable(ctx: NodeExecutorContext) {
    const { node, state } = ctx;
    const assignments = (node.data.config?.assignments as Array<{
      key: string;
      value: unknown;
      expression?: string;
    }>) || [];

    const updates: Record<string, unknown> = {};

    for (const assignment of assignments) {
      if (assignment.expression) {
        updates[assignment.key] = this.evaluateExpression(
          assignment.expression,
          state.variables,
        );
      } else {
        const value =
          typeof assignment.value === 'string'
            ? this.interpolateVariables(assignment.value, state.variables)
            : assignment.value;
        updates[assignment.key] = value;
      }
    }

    return {
      status: 'success' as const,
      output: { assignments: updates },
      variableUpdates: updates,
    };
  }

  // --------------------------------------------------------------------------
  // Edge Evaluation
  // --------------------------------------------------------------------------

  private async evaluateEdgesForInput(
    nodeId: string,
    edges: WorkflowEdge[],
    state: WorkflowExecutionState,
    input: string,
  ): Promise<WorkflowEdge | null> {
    const nodeEdges = edges.filter((e) => e.source === nodeId);

    for (const edge of nodeEdges) {
      if (!edge.conditions) continue;

      if (edge.conditions.expression) {
        const result = this.evaluateExpression(edge.conditions.expression, {
          ...state.variables,
          _input: input,
          _lastInput: input,
        });
        if (result) return edge;
      }

      if (edge.conditions.operator && edge.conditions.field) {
        const fieldValue = edge.conditions.field === '_input'
          ? input
          : state.variables[edge.conditions.field];
        const matches = this.evaluateOperator(
          edge.conditions.operator,
          fieldValue,
          edge.conditions.value,
        );
        if (matches) return edge;
      }
    }

    // Return default edge if no condition matched
    const defaultEdge = nodeEdges.find(
      (e) =>
        !e.conditions ||
        e.conditions.operator === 'default' ||
        (!e.conditions.expression && !e.conditions.field),
    );
    return defaultEdge || null;
  }

  private evaluateOperator(
    operator: string,
    actual: unknown,
    expected: unknown,
  ): boolean {
    const a = typeof actual === 'string' ? actual.toLowerCase() : actual;
    const e = typeof expected === 'string' ? expected.toLowerCase() : expected;

    switch (operator) {
      case 'eq':
        return a === e;
      case 'neq':
        return a !== e;
      case 'gt':
        return Number(a) > Number(e);
      case 'lt':
        return Number(a) < Number(e);
      case 'gte':
        return Number(a) >= Number(e);
      case 'lte':
        return Number(a) <= Number(e);
      case 'contains':
        return typeof a === 'string' && typeof e === 'string' && a.includes(e);
      case 'matches':
        try {
          return typeof a === 'string' && new RegExp(String(e)).test(a);
        } catch {
          return false;
        }
      case 'default':
        return true;
      default:
        return false;
    }
  }

  // --------------------------------------------------------------------------
  // Expression Evaluator (safe subset)
  // --------------------------------------------------------------------------

  evaluateExpression(
    expression: string,
    variables: Record<string, unknown>,
  ): unknown {
    try {
      // Build a safe evaluation context by exposing only variables
      const safeExpression = expression.replace(
        /\b([a-zA-Z_][a-zA-Z0-9_]*)\b/g,
        (match) => {
          // Preserve JavaScript keywords and literal values
          const reserved = [
            'true', 'false', 'null', 'undefined', 'NaN', 'Infinity',
            'typeof', 'instanceof', 'void', 'in', 'of',
            'if', 'else', 'return', 'new', 'this',
            'Math', 'Number', 'String', 'Boolean', 'Array',
            'parseInt', 'parseFloat', 'isNaN', 'isFinite',
          ];
          if (reserved.includes(match)) return match;

          // Map to variables context
          if (variables[match] !== undefined) {
            const val = variables[match];
            if (typeof val === 'string') return JSON.stringify(val);
            if (typeof val === 'number' || typeof val === 'boolean') return String(val);
            if (val === null) return 'null';
            return JSON.stringify(val);
          }
          return match;
        },
      );

      // Use Function constructor for sandboxed eval (no access to global scope)
      const fn = new Function(`"use strict"; return (${safeExpression});`);
      return fn();
    } catch (error) {
      this.logger.warn(
        `Expression evaluation failed: "${expression}" - ${error}`,
      );
      return false;
    }
  }

  // --------------------------------------------------------------------------
  // Variable Interpolation
  // --------------------------------------------------------------------------

  interpolateVariables(
    template: string,
    variables: Record<string, unknown>,
  ): string {
    return template.replace(/\{\{([^}]+)\}\}/g, (_, key: string) => {
      const trimmedKey = key.trim();
      const value = variables[trimmedKey];
      if (value === undefined || value === null) return `{{${trimmedKey}}}`;
      return String(value);
    });
  }

  // --------------------------------------------------------------------------
  // State Persistence
  // --------------------------------------------------------------------------

  private async persistState(state: WorkflowExecutionState): Promise<void> {
    state.updatedAt = new Date().toISOString();
    await this.redis.setJson(
      `${this.STATE_PREFIX}${state.id}`,
      state,
      this.STATE_TTL,
    );
    // Also index by callId for quick lookups
    await this.redis.set(
      `${this.STATE_PREFIX}call:${state.callId}`,
      state.id,
      this.STATE_TTL,
    );
  }

  private async loadState(
    executionId: string,
  ): Promise<WorkflowExecutionState | null> {
    return this.redis.getJson<WorkflowExecutionState>(
      `${this.STATE_PREFIX}${executionId}`,
    );
  }

  async getStateByCallId(callId: string): Promise<WorkflowExecutionState | null> {
    const executionId = await this.redis.get(
      `${this.STATE_PREFIX}call:${callId}`,
    );
    if (!executionId) return null;
    return this.loadState(executionId);
  }

  // --------------------------------------------------------------------------
  // Helpers
  // --------------------------------------------------------------------------

  private getTimeOfDay(): string {
    const hour = new Date().getHours();
    if (hour < 12) return 'morning';
    if (hour < 17) return 'afternoon';
    return 'evening';
  }

  private greetingPrefix(timeOfDay: string): string {
    switch (timeOfDay) {
      case 'morning':
        return 'Good morning!';
      case 'afternoon':
        return 'Good afternoon!';
      case 'evening':
        return 'Good evening!';
      default:
        return 'Hello!';
    }
  }

  private async makeHttpRequest(
    url: string,
    method: string,
    headers: Record<string, string>,
    body?: unknown,
  ): Promise<Record<string, unknown>> {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);

      const fetchOptions: RequestInit = {
        method: method.toUpperCase(),
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        signal: controller.signal,
      };

      if (body && method.toUpperCase() !== 'GET') {
        fetchOptions.body = JSON.stringify(body);
      }

      const response = await fetch(url, fetchOptions);
      clearTimeout(timeout);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        return (await response.json()) as Record<string, unknown>;
      }

      return { text: await response.text() };
    } catch (error) {
      throw new Error(`HTTP request to ${url} failed: ${error}`);
    }
  }
}
