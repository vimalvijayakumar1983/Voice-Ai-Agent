import { Injectable, NotFoundException, ConflictException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { CreateTenantDto, UpdateTenantDto, TenantSettingsDto } from './dto/create-tenant.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

@Injectable()
export class TenantsService {
  private readonly logger = new Logger(TenantsService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const [tenants, total] = await Promise.all([
      this.prisma.tenant.findMany({
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        include: { _count: { select: { users: true, agents: true } } },
      }),
      this.prisma.tenant.count(),
    ]);

    return new PaginatedResponse(tenants, total, query);
  }

  async findById(id: string): Promise<any> {
    const tenant = await this.prisma.tenant.findUnique({
      where: { id },
      include: {
        _count: {
          select: { users: true, agents: true, campaigns: true },
        },
      },
    });

    if (!tenant) throw new NotFoundException(`Tenant ${id} not found`);
    return tenant;
  }

  async create(dto: CreateTenantDto): Promise<any> {
    if (dto.slug) {
      const existing = await this.prisma.tenant.findUnique({
        where: { slug: dto.slug },
      });
      if (existing) throw new ConflictException('Slug already in use');
    }

    const slug = dto.slug || this.generateSlug(dto.name);

    return this.prisma.tenant.create({
      data: {
        name: dto.name,
        slug,
        industry: dto.industry || 'OTHER',
        description: dto.description,
        website: dto.website,
        logoUrl: dto.logoUrl,
        settings: {},
      },
    });
  }

  async update(id: string, dto: UpdateTenantDto): Promise<any> {
    await this.findById(id);

    return this.prisma.tenant.update({
      where: { id },
      data: dto,
    });
  }

  async updateSettings(id: string, dto: TenantSettingsDto): Promise<any> {
    const tenant = await this.findById(id);
    const currentSettings = (tenant.settings as Record<string, unknown>) || {};

    return this.prisma.tenant.update({
      where: { id },
      data: {
        settings: { ...currentSettings, ...dto },
      },
    });
  }

  async delete(id: string): Promise<void> {
    await this.findById(id);
    await this.prisma.tenant.update({
      where: { id },
      data: { isActive: false },
    });
    this.logger.log(`Tenant ${id} deactivated`);
  }

  async getMembers(tenantId: string, query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const [users, total] = await Promise.all([
      this.prisma.user.findMany({
        where: { tenantId },
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        select: {
          id: true,
          email: true,
          firstName: true,
          lastName: true,
          role: true,
          isActive: true,
          createdAt: true,
          lastLoginAt: true,
        },
      }),
      this.prisma.user.count({ where: { tenantId } }),
    ]);

    return new PaginatedResponse(users, total, query);
  }

  private generateSlug(name: string): string {
    return name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '');
  }
}
