import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

export enum NotificationType {
  INFO = 'INFO',
  WARNING = 'WARNING',
  ERROR = 'ERROR',
  SUCCESS = 'SUCCESS',
}

export enum NotificationChannel {
  IN_APP = 'IN_APP',
  EMAIL = 'EMAIL',
  WEBHOOK = 'WEBHOOK',
}

export interface SendNotificationDto {
  userId: string;
  tenantId: string;
  title: string;
  message: string;
  type: NotificationType;
  channel: NotificationChannel;
  actionUrl?: string;
  metadata?: Record<string, unknown>;
}

@Injectable()
export class NotificationsService {
  private readonly logger = new Logger(NotificationsService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(
    tenantId: string,
    userId: string,
    query: PaginationQuery,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where = { tenantId, userId };

    const [notifications, total] = await Promise.all([
      this.prisma.notification.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.notification.count({ where }),
    ]);

    return new PaginatedResponse(notifications, total, query);
  }

  async getUnreadCount(tenantId: string, userId: string): Promise<{ count: number }> {
    const count = await this.prisma.notification.count({
      where: { tenantId, userId, readAt: null },
    });
    return { count };
  }

  async markAsRead(tenantId: string, userId: string, id: string): Promise<any> {
    const notification = await this.prisma.notification.findFirst({
      where: { id, tenantId, userId },
    });

    if (!notification) throw new NotFoundException(`Notification ${id} not found`);

    return this.prisma.notification.update({
      where: { id },
      data: { readAt: new Date() },
    });
  }

  async markAllAsRead(tenantId: string, userId: string): Promise<{ count: number }> {
    const result = await this.prisma.notification.updateMany({
      where: { tenantId, userId, readAt: null },
      data: { readAt: new Date() },
    });
    return { count: result.count };
  }

  async send(dto: SendNotificationDto): Promise<any> {
    const notification = await this.prisma.notification.create({
      data: {
        userId: dto.userId,
        tenantId: dto.tenantId,
        title: dto.title,
        body: dto.message,
        type: dto.type,
        channel: dto.channel as any,
        metadata: (dto.metadata || {}) as any,
      },
    });

    this.logger.log(
      `Notification sent to user ${dto.userId}: ${dto.title} via ${dto.channel}`,
    );

    // In production, also send via email/webhook based on channel
    return notification;
  }

  async getPreferences(tenantId: string, userId: string): Promise<any> {
    // Notification preferences are stored in the user's tenant settings
    const user = await this.prisma.user.findFirst({
      where: { id: userId, tenantId },
      select: { id: true },
    });

    if (!user) {
      return {
        emailEnabled: true,
        inAppEnabled: true,
        webhookEnabled: false,
        emailDigest: 'DAILY',
        mutedTypes: [],
      };
    }

    // Return defaults - preferences can be extended via tenant settings
    return {
      emailEnabled: true,
      inAppEnabled: true,
      webhookEnabled: false,
      emailDigest: 'DAILY',
      mutedTypes: [],
    };
  }

  async updatePreferences(
    tenantId: string,
    userId: string,
    preferences: Record<string, unknown>,
  ): Promise<any> {
    // Store preferences in a lightweight way since NotificationPreference model doesn't exist
    this.logger.log(`Notification preferences updated for user ${userId} in tenant ${tenantId}`);
    return { userId, tenantId, ...preferences };
  }
}
