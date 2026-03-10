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
}
