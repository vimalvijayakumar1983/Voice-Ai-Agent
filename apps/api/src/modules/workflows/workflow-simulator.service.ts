import { Injectable, Logger, NotFoundException } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { WorkflowEngineService, WorkflowNodeType, WorkflowExecutionState } from './workflow-engine.service';
import { WorkflowValidatorService, ValidationResult } from './workflow-validator.service';

// ============================================================================
// Types
// ============================================================================

export interface SimulationConfig {
  workflowId: string;
  tenantId: string;
  inputs: SimulationInput[];
  initialVariables?: Record<string, unknown>;
  maxSteps?: number;
}

export interface SimulationInput {
  stepIndex: number;
  input: string;
}

export interface SimulationStep {
  stepIndex: number;
  nodeId: string;
  nodeType: string;
  nodeLabel: string;
  agentMessage?: string;
  contactInput?: string;
  variablesSnapshot: Record<string, unknown>;
  selectedEdgeId?: string;
  selectedEdgeLabel?: string;
  status: 'executed' | 'waiting' | 'failed' | 'completed';
  duration: number;
  output: Record<string, unknown>;
}

export interface SimulationResult {
  id: string;
  workflowId: string;
  steps: SimulationStep[];
  finalVariables: Record<string, unknown>;
  finalStatus: string;
  totalSteps: number;
  conversationPreview: Array<{ role: string; content: string }>;
  validation: ValidationResult;
  startedAt: string;
  completedAt: string;
}

export interface TestScenario {
  id: string;
  name: string;
  description: string;
  inputs: SimulationInput[];
  expectedOutcome?: string;
  initialVariables?: Record<string, unknown>;
}

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class WorkflowSimulatorService {
  private readonly logger = new Logger(WorkflowSimulatorService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly engine: WorkflowEngineService,
    private readonly validator: WorkflowValidatorService,
  ) {}

  // --------------------------------------------------------------------------
  // Dry-Run Simulation
  // --------------------------------------------------------------------------

  async simulate(config: SimulationConfig): Promise<SimulationResult> {
    const startedAt = new Date().toISOString();
    const maxSteps = config.maxSteps || 50;

    const workflow = await this.prisma.workflow.findFirst({
      where: { id: config.workflowId, tenantId: config.tenantId },
    });
    if (!workflow) {
      throw new NotFoundException(`Workflow ${config.workflowId} not found`);
    }

    const nodes = workflow.nodes as unknown as Array<{
      id: string;
      type: string;
      position: { x: number; y: number };
      data: Record<string, unknown>;
    }>;
    const edges = workflow.edges as unknown as Array<{
      id: string;
      source: string;
      target: string;
      label?: string;
      conditions?: Record<string, unknown>;
    }>;

    // Validate first
    const validation = this.validator.validate(nodes, edges);

    const steps: SimulationStep[] = [];
    const conversationPreview: Array<{ role: string; content: string }> = [];
    let inputIndex = 0;

    // Create a simulated call ID
    const simulatedCallId = `sim_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;

    // Start execution
    let state = await this.engine.startExecution(
      config.workflowId,
      simulatedCallId,
      config.tenantId,
      {
        ...config.initialVariables,
        _isSimulation: true,
      },
    );

    // Record initial step
    this.recordStep(steps, conversationPreview, state, nodes, edges, 0);

    let stepCount = 1;

    // Process each input
    while (
      stepCount < maxSteps &&
      state.status !== 'completed' &&
      state.status !== 'failed'
    ) {
      if (state.status === 'waiting') {
        // Find the next input for this step
        const simInput = config.inputs.find((i) => i.stepIndex === inputIndex);
        if (!simInput) {
          // No more inputs, use a default
          const defaultInput = this.generateDefaultInput(
            state.currentNodeId,
            nodes,
          );
          if (!defaultInput) break;

          state = await this.engine.processInput(state.id, defaultInput);
          conversationPreview.push({ role: 'contact', content: defaultInput });
        } else {
          state = await this.engine.processInput(state.id, simInput.input);
          conversationPreview.push({ role: 'contact', content: simInput.input });
        }
        inputIndex++;
      } else {
        // Auto-advance (shouldn't happen often, but handles edge cases)
        break;
      }

      this.recordStep(steps, conversationPreview, state, nodes, edges, stepCount);
      stepCount++;
    }

    return {
      id: simulatedCallId,
      workflowId: config.workflowId,
      steps,
      finalVariables: state.variables,
      finalStatus: state.status,
      totalSteps: steps.length,
      conversationPreview,
      validation,
      startedAt,
      completedAt: new Date().toISOString(),
    };
  }

  // --------------------------------------------------------------------------
  // Step-by-Step Execution
  // --------------------------------------------------------------------------

  async startStepByStep(
    workflowId: string,
    tenantId: string,
    initialVariables?: Record<string, unknown>,
  ): Promise<{ executionId: string; step: SimulationStep; state: WorkflowExecutionState }> {
    const simulatedCallId = `sim_step_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;

    const state = await this.engine.startExecution(
      workflowId,
      simulatedCallId,
      tenantId,
      { ...initialVariables, _isSimulation: true },
    );

    const workflow = await this.prisma.workflow.findFirst({
      where: { id: workflowId, tenantId },
    });

    const nodes = (workflow?.nodes || []) as unknown as Array<{
      id: string;
      type: string;
      position: { x: number; y: number };
      data: Record<string, unknown>;
    }>;

    const currentNode = nodes.find((n) => n.id === state.currentNodeId);

    const step: SimulationStep = {
      stepIndex: 0,
      nodeId: state.currentNodeId,
      nodeType: currentNode?.type || 'unknown',
      nodeLabel: (currentNode?.data.label as string) || state.currentNodeId,
      agentMessage: state.conversationHistory.find(
        (t) => t.role === 'agent' && t.nodeId === state.currentNodeId,
      )?.content,
      variablesSnapshot: { ...state.variables },
      status: state.status === 'waiting' ? 'waiting' : 'executed',
      duration: 0,
      output: state.nodeResults[state.currentNodeId]?.output || {},
    };

    return { executionId: state.id, step, state };
  }

  async advanceStep(
    executionId: string,
    input: string,
  ): Promise<{ step: SimulationStep; state: WorkflowExecutionState }> {
    const state = await this.engine.processInput(executionId, input);

    const workflow = await this.prisma.workflow.findFirst({
      where: { id: state.workflowId, tenantId: state.tenantId },
    });

    const nodes = (workflow?.nodes || []) as unknown as Array<{
      id: string;
      type: string;
      position: { x: number; y: number };
      data: Record<string, unknown>;
    }>;

    const currentNode = nodes.find((n) => n.id === state.currentNodeId);
    const nodeResult = state.nodeResults[state.currentNodeId];

    const step: SimulationStep = {
      stepIndex: state.previousNodeIds.length,
      nodeId: state.currentNodeId,
      nodeType: currentNode?.type || 'unknown',
      nodeLabel: (currentNode?.data.label as string) || state.currentNodeId,
      agentMessage: state.conversationHistory
        .filter((t) => t.role === 'agent' && t.nodeId === state.currentNodeId)
        .pop()?.content,
      contactInput: input,
      variablesSnapshot: { ...state.variables },
      selectedEdgeId: nodeResult?.selectedEdge,
      status:
        state.status === 'completed'
          ? 'completed'
          : state.status === 'failed'
            ? 'failed'
            : state.status === 'waiting'
              ? 'waiting'
              : 'executed',
      duration: nodeResult?.duration || 0,
      output: nodeResult?.output || {},
    };

    return { step, state };
  }

  // --------------------------------------------------------------------------
  // Test Scenario Generation
  // --------------------------------------------------------------------------

  async generateTestScenarios(
    workflowId: string,
    tenantId: string,
  ): Promise<TestScenario[]> {
    const workflow = await this.prisma.workflow.findFirst({
      where: { id: workflowId, tenantId },
    });
    if (!workflow) {
      throw new NotFoundException(`Workflow ${workflowId} not found`);
    }

    const nodes = workflow.nodes as unknown as Array<{
      id: string;
      type: string;
      data: Record<string, unknown>;
    }>;
    const edges = workflow.edges as unknown as Array<{
      id: string;
      source: string;
      target: string;
      label?: string;
      conditions?: Record<string, unknown>;
    }>;

    const scenarios: TestScenario[] = [];

    // Happy path scenario
    scenarios.push(this.generateHappyPath(nodes, edges));

    // Consent denied path
    if (nodes.some((n) => n.type === 'CONSENT_CAPTURE')) {
      scenarios.push(this.generateConsentDenied(nodes, edges));
    }

    // Verification failure path
    if (nodes.some((n) => n.type === 'VERIFICATION')) {
      scenarios.push(this.generateVerificationFailure(nodes, edges));
    }

    // Low qualification score path
    if (nodes.some((n) => n.type === 'QUALIFICATION')) {
      scenarios.push(this.generateLowQualification(nodes, edges));
    }

    // Escalation path
    if (nodes.some((n) => n.type === 'ESCALATE_HUMAN')) {
      scenarios.push(this.generateEscalationScenario(nodes, edges));
    }

    return scenarios;
  }

  // --------------------------------------------------------------------------
  // Variable Inspection
  // --------------------------------------------------------------------------

  async inspectVariables(
    executionId: string,
  ): Promise<{
    variables: Record<string, unknown>;
    systemVariables: Record<string, unknown>;
    capturedData: Record<string, unknown>;
    qualificationData: Record<string, unknown>;
  }> {
    const state = await this.engine.getExecutionState(executionId);
    if (!state) {
      throw new NotFoundException(`Execution ${executionId} not found`);
    }

    const systemVariables: Record<string, unknown> = {};
    const capturedData: Record<string, unknown> = {};
    const qualificationData: Record<string, unknown> = {};
    const userVariables: Record<string, unknown> = {};

    for (const [key, value] of Object.entries(state.variables)) {
      if (key.startsWith('_')) {
        if (key.startsWith('_captured_')) {
          capturedData[key.replace('_captured_', '')] = value;
        } else if (key.startsWith('_qual_')) {
          qualificationData[key.replace('_qual_', '')] = value;
        } else {
          systemVariables[key] = value;
        }
      } else {
        userVariables[key] = value;
      }
    }

    return {
      variables: userVariables,
      systemVariables,
      capturedData,
      qualificationData,
    };
  }

  // --------------------------------------------------------------------------
  // Private Helpers
  // --------------------------------------------------------------------------

  private recordStep(
    steps: SimulationStep[],
    conversationPreview: Array<{ role: string; content: string }>,
    state: WorkflowExecutionState,
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
    stepIndex: number,
  ): void {
    const currentNode = nodes.find((n) => n.id === state.currentNodeId);
    const nodeResult = state.nodeResults[state.currentNodeId];

    // Add agent messages to conversation preview
    const latestAgentMessage = state.conversationHistory
      .filter((t) => t.role === 'agent' && t.nodeId === state.currentNodeId)
      .pop();
    if (latestAgentMessage) {
      conversationPreview.push({
        role: 'agent',
        content: latestAgentMessage.content,
      });
    }

    const selectedEdge = nodeResult?.selectedEdge
      ? edges.find((e) => e.id === nodeResult.selectedEdge)
      : undefined;

    steps.push({
      stepIndex,
      nodeId: state.currentNodeId,
      nodeType: currentNode?.type || 'unknown',
      nodeLabel: (currentNode?.data.label as string) || state.currentNodeId,
      agentMessage: latestAgentMessage?.content,
      variablesSnapshot: { ...state.variables },
      selectedEdgeId: selectedEdge?.id,
      selectedEdgeLabel: selectedEdge?.label,
      status:
        state.status === 'completed'
          ? 'completed'
          : state.status === 'failed'
            ? 'failed'
            : state.status === 'waiting'
              ? 'waiting'
              : 'executed',
      duration: nodeResult?.duration || 0,
      output: nodeResult?.output || {},
    });
  }

  private generateDefaultInput(
    nodeId: string,
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
  ): string | null {
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return null;

    switch (node.type) {
      case WorkflowNodeType.CONSENT_CAPTURE:
        return 'Yes, I consent';
      case WorkflowNodeType.VERIFICATION:
        return '01/15/1990';
      case WorkflowNodeType.QUALIFICATION:
        return 'Yes';
      case WorkflowNodeType.INFO_CAPTURE:
        return 'John Doe';
      case WorkflowNodeType.OFFER_OPTIONS:
        return '1';
      case WorkflowNodeType.BOOK_APPOINTMENT:
        return 'Tomorrow at 2 PM';
      case WorkflowNodeType.WAIT:
        return 'Continue';
      default:
        return 'Yes';
    }
  }

  private generateHappyPath(
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
  ): TestScenario {
    const inputs: SimulationInput[] = [];
    let stepIndex = 0;

    for (const node of nodes) {
      const defaultInput = this.generateDefaultInput(node.id, nodes);
      if (defaultInput) {
        inputs.push({ stepIndex, input: defaultInput });
        stepIndex++;
      }
    }

    return {
      id: `scenario_happy_${Date.now()}`,
      name: 'Happy Path',
      description:
        'Simulates a successful flow where the contact provides all expected responses positively.',
      inputs,
      expectedOutcome: 'completed',
    };
  }

  private generateConsentDenied(
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
  ): TestScenario {
    const inputs: SimulationInput[] = [];
    let stepIndex = 0;

    for (const node of nodes) {
      if (node.type === WorkflowNodeType.CONSENT_CAPTURE) {
        inputs.push({ stepIndex, input: 'No, I do not consent' });
        stepIndex++;
        break;
      }
      const defaultInput = this.generateDefaultInput(node.id, nodes);
      if (defaultInput) {
        inputs.push({ stepIndex, input: defaultInput });
        stepIndex++;
      }
    }

    return {
      id: `scenario_consent_denied_${Date.now()}`,
      name: 'Consent Denied',
      description: 'Contact declines consent to proceed.',
      inputs,
      expectedOutcome: 'completed',
    };
  }

  private generateVerificationFailure(
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
  ): TestScenario {
    const inputs: SimulationInput[] = [];
    let stepIndex = 0;

    for (const node of nodes) {
      if (node.type === WorkflowNodeType.VERIFICATION) {
        inputs.push({ stepIndex, input: '00/00/0000' });
        stepIndex++;
        break;
      }
      const defaultInput = this.generateDefaultInput(node.id, nodes);
      if (defaultInput) {
        inputs.push({ stepIndex, input: defaultInput });
        stepIndex++;
      }
    }

    return {
      id: `scenario_verification_fail_${Date.now()}`,
      name: 'Verification Failure',
      description: 'Contact fails identity verification.',
      inputs,
      expectedOutcome: 'completed',
    };
  }

  private generateLowQualification(
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
  ): TestScenario {
    const inputs: SimulationInput[] = [];
    let stepIndex = 0;

    for (const node of nodes) {
      if (node.type === WorkflowNodeType.QUALIFICATION) {
        // Answer negatively to qualification questions
        const questions = (node.data.config as Record<string, unknown>)?.questions as unknown[] | undefined;
        const count = questions?.length || 3;
        for (let i = 0; i < count; i++) {
          inputs.push({ stepIndex, input: 'No' });
          stepIndex++;
        }
        break;
      }
      const defaultInput = this.generateDefaultInput(node.id, nodes);
      if (defaultInput) {
        inputs.push({ stepIndex, input: defaultInput });
        stepIndex++;
      }
    }

    return {
      id: `scenario_low_qual_${Date.now()}`,
      name: 'Low Qualification Score',
      description: 'Contact answers qualification questions negatively.',
      inputs,
      expectedOutcome: 'completed',
    };
  }

  private generateEscalationScenario(
    nodes: Array<{ id: string; type: string; data: Record<string, unknown> }>,
    edges: Array<{ id: string; source: string; target: string; label?: string }>,
  ): TestScenario {
    const inputs: SimulationInput[] = [];
    let stepIndex = 0;

    for (const node of nodes) {
      const defaultInput = this.generateDefaultInput(node.id, nodes);
      if (defaultInput) {
        inputs.push({ stepIndex, input: 'I want to speak to a human agent' });
        stepIndex++;
        break;
      }
    }

    return {
      id: `scenario_escalation_${Date.now()}`,
      name: 'Human Escalation',
      description: 'Contact requests to speak with a human agent.',
      inputs,
      expectedOutcome: 'completed',
    };
  }
}
