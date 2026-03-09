import {
  Injectable,
  NotFoundException,
  ConflictException,
  ForbiddenException,
  Logger,
} from '@nestjs/common';
import * as bcrypt from 'bcrypt';
import { PrismaService } from '../../common/services/prisma.service';
import { CreateUserDto, UpdateUserDto, InviteUserDto } from './dto/create-user.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';
import { v4 as uuidv4 } from 'uuid';

@Injectable()
export class UsersService {
  private readonly logger = new Logger(UsersService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where = this.prisma.tenantWhere(tenantId);

    const [users, total] = await Promise.all([
      this.prisma.user.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        select: {
          id: true,
          email: true,
          firstName: true,
          lastName: true,
          role: true,
          phone: true,
          isActive: true,
          createdAt: true,
          lastLoginAt: true,
        },
      }),
      this.prisma.user.count({ where }),
    ]);

    return new PaginatedResponse(users, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const user = await this.prisma.user.findFirst({
      where: { id, tenantId },
      select: {
        id: true,
        email: true,
        firstName: true,
        lastName: true,
        role: true,
        phone: true,
        isActive: true,
        createdAt: true,
        updatedAt: true,
        lastLoginAt: true,
      },
    });

    if (!user) throw new NotFoundException(`User ${id} not found`);
    return user;
  }

  async create(tenantId: string, dto: CreateUserDto): Promise<any> {
    const existing = await this.prisma.user.findUnique({
      where: { email: dto.email },
    });

    if (existing) throw new ConflictException('Email already registered');

    const hashedPassword = await bcrypt.hash(dto.password, 12);

    const user = await this.prisma.user.create({
      data: {
        email: dto.email,
        passwordHash: hashedPassword,
        firstName: dto.firstName,
        lastName: dto.lastName,
        role: dto.role || 'AGENT',
        phone: dto.phone || null,
        tenantId,
        isActive: true,
      },
      select: {
        id: true,
        email: true,
        firstName: true,
        lastName: true,
        role: true,
        createdAt: true,
      },
    });

    this.logger.log(`User ${user.id} created for tenant ${tenantId}`);
    return user;
  }

  async update(tenantId: string, id: string, dto: UpdateUserDto): Promise<any> {
    await this.findById(tenantId, id);

    return this.prisma.user.update({
      where: { id },
      data: dto,
      select: {
        id: true,
        email: true,
        firstName: true,
        lastName: true,
        role: true,
        phone: true,
        isActive: true,
        updatedAt: true,
      },
    });
  }

  async assignRole(tenantId: string, id: string, role: string): Promise<any> {
    const user = await this.findById(tenantId, id);

    if (user.role === 'TENANT_OWNER') {
      throw new ForbiddenException('Cannot change role of tenant owner');
    }

    return this.prisma.user.update({
      where: { id },
      data: { role },
      select: { id: true, email: true, role: true },
    });
  }

  async inviteUser(tenantId: string, dto: InviteUserDto): Promise<any> {
    const existing = await this.prisma.user.findUnique({
      where: { email: dto.email },
    });

    if (existing) throw new ConflictException('Email already registered');

    const tempPassword = uuidv4().slice(0, 12);
    const hashedPassword = await bcrypt.hash(tempPassword, 12);

    const user = await this.prisma.user.create({
      data: {
        email: dto.email,
        passwordHash: hashedPassword,
        firstName: dto.firstName || '',
        lastName: dto.lastName || '',
        role: dto.role,
        tenantId,
        isActive: true,
      },
      select: {
        id: true,
        email: true,
        role: true,
        createdAt: true,
      },
    });

    this.logger.log(`User ${dto.email} invited to tenant ${tenantId}`);
    // In production, send invitation email with temp password or magic link
    return { ...user, invited: true };
  }

  async deactivate(tenantId: string, id: string): Promise<void> {
    const user = await this.findById(tenantId, id);

    if (user.role === 'TENANT_OWNER') {
      throw new ForbiddenException('Cannot deactivate tenant owner');
    }

    await this.prisma.user.update({
      where: { id },
      data: { isActive: false },
    });

    this.logger.log(`User ${id} deactivated`);
  }
}
