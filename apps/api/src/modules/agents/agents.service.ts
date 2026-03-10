import {
  Injectable,
  NotFoundException,
  BadRequestException,
  Logger,
} from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { CreateAgentDto } from './dto/create-agent.dto';
import { UpdateAgentDto } from './dto/update-agent.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

@Injectable()
export class AgentsService {
  private readonly logger = new Logger(AgentsService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;
    const where = this.prisma.tenantWhere(tenantId);

    const [agents, total] = await Promise.all([
      this.prisma.agent.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.agent.count({ where }),
    ]);

    return new PaginatedResponse(agents, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const agent = await this.prisma.agent.findFirst({
      where: { id, tenantId },
    });

    if (!agent) throw new NotFoundException(`Agent ${id} not found`);
    return agent;
  }

  async create(tenantId: string, userId: string, dto: CreateAgentDto): Promise<any> {
    const { escalationRules, complianceBlocks, successCriteria, tools, metadata, ...rest } = dto as any;
    const agent = await this.prisma.agent.create({
      data: {
        ...rest,
        tenantId,
        createdById: userId,
        version: 1,
        isActive: true,
        escalationRules: escalationRules ? JSON.parse(JSON.stringify(escalationRules)) : [],
        complianceBlocks: complianceBlocks ? JSON.parse(JSON.stringify(complianceBlocks)) : [],
        successCriteria: successCriteria || null,
        tools: tools ? JSON.parse(JSON.stringify(tools)) : [],
        metadata: metadata || {},
      },
    });

    this.logger.log(`Agent ${agent.id} created for tenant ${tenantId}`);
    return agent;
  }

  async update(tenantId: string, id: string, dto: UpdateAgentDto): Promise<any> {
    await this.findById(tenantId, id);

    const updateData: Record<string, unknown> = { ...dto };
    if (dto.escalationRules) {
      updateData.escalationRules = JSON.parse(JSON.stringify(dto.escalationRules));
    }
    if (dto.complianceBlocks) {
      updateData.complianceBlocks = JSON.parse(JSON.stringify(dto.complianceBlocks));
    }
    if (dto.tools) {
      updateData.tools = JSON.parse(JSON.stringify(dto.tools));
    }

    return this.prisma.agent.update({
      where: { id },
      data: updateData,
    });
  }

  async delete(tenantId: string, id: string): Promise<void> {
    await this.findById(tenantId, id);
    await this.prisma.agent.update({
      where: { id },
      data: { isActive: false },
    });
    this.logger.log(`Agent ${id} deactivated`);
  }

  async createVersion(tenantId: string, id: string): Promise<any> {
    const agent = await this.findById(tenantId, id);
    const newVersion = (agent.version || 1) + 1;

    const versionRecord = await this.prisma.agentVersion.create({
      data: {
        agentId: id,
        version: agent.version,
        config: JSON.parse(JSON.stringify(agent)),
        tenantId,
      },
    });

    await this.prisma.agent.update({
      where: { id },
      data: { version: newVersion },
    });

    this.logger.log(`Agent ${id} version ${newVersion} created`);
    return versionRecord;
  }

  async getVersions(tenantId: string, id: string): Promise<any[]> {
    await this.findById(tenantId, id);

    return this.prisma.agentVersion.findMany({
      where: { agentId: id, tenantId },
      orderBy: { version: 'desc' },
    });
  }

  async cloneAgent(tenantId: string, id: string, newName: string): Promise<any> {
    const source = await this.findById(tenantId, id);

    const { id: _id, createdAt, updatedAt, ...sourceData } = source;

    const cloned = await this.prisma.agent.create({
      data: {
        ...sourceData,
        name: newName || `${source.name} (Copy)`,
        version: 1,
        tenantId,
      },
    });

    this.logger.log(`Agent ${id} cloned as ${cloned.id}`);
    return cloned;
  }

  async testAgent(tenantId: string, id: string, testInput: string): Promise<any> {
    const agent = await this.findById(tenantId, id);

    if (!agent.isActive) {
      throw new BadRequestException('Cannot test inactive agent');
    }

    // In production, this would invoke the LLM with the agent's config
    return {
      agentId: id,
      input: testInput,
      output: `[Test response from agent "${agent.name}" - LLM integration pending]`,
      systemPrompt: agent.systemPrompt,
      model: agent.llmModel || 'default',
      timestamp: new Date().toISOString(),
    };
  }

  /**
   * Initiates a demo call to test an agent's voice, speed, and conversational ability.
   * Creates a call record tagged as a demo call and triggers the telephony provider.
   */
  async initiateDemoCall(
    tenantId: string,
    userId: string,
    agentId: string,
    toNumber: string,
    fromNumber?: string,
    voice?: string,
    language?: string,
  ): Promise<any> {
    const agent = await this.findById(tenantId, agentId);

    if (!agent.isActive) {
      throw new BadRequestException('Cannot demo an inactive agent');
    }

    // Use overrides from the demo call dialog, or fall back to agent defaults
    const callVoice = voice || agent.voice || 'nova';
    const callLanguage = language || agent.language || 'en-US';

    // Get a from number: use provided, agent default, or tenant phone number
    let callerNumber = fromNumber;
    if (!callerNumber) {
      const phoneNumber = await this.prisma.phoneNumber.findFirst({
        where: { tenantId, isActive: true },
        orderBy: { createdAt: 'desc' },
      });
      callerNumber = phoneNumber?.number;
    }

    if (!callerNumber) {
      throw new BadRequestException(
        'No phone number available. Please provide a fromNumber or configure a phone number in Settings > Phone Numbers.',
      );
    }

    // Create the call record tagged as demo with voice/language overrides
    const call = await this.prisma.call.create({
      data: {
        tenantId,
        agentId,
        direction: 'OUTBOUND',
        status: 'QUEUED',
        toNumber,
        fromNumber: callerNumber,
        initiatedById: userId,
        metadata: {
          isDemoCall: true,
          demoInitiatedBy: userId,
          agentName: agent.name,
          voice: callVoice,
          language: callLanguage,
        },
      },
    });

    this.logger.log(
      `Demo call ${call.id} initiated for agent "${agent.name}" (${agentId}) to ${toNumber} [voice=${callVoice}, lang=${callLanguage}]`,
    );

    return {
      callId: call.id,
      agentId,
      agentName: agent.name,
      toNumber,
      fromNumber: callerNumber,
      status: 'QUEUED',
      isDemoCall: true,
      message: `Demo call queued. You will receive a call at ${toNumber} from agent "${agent.name}". Answer the call to test the agent's voice, speed, and conversational ability.`,
      agentConfig: {
        voice: callVoice,
        language: callLanguage,
        llmModel: agent.llmModel || 'default',
        systemPromptPreview: agent.systemPrompt
          ? agent.systemPrompt.substring(0, 200) + (agent.systemPrompt.length > 200 ? '...' : '')
          : null,
      },
    };
  }
}
