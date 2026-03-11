import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { ThrottlerModule } from '@nestjs/throttler';
import { BullModule } from '@nestjs/bullmq';
import { ScheduleModule } from '@nestjs/schedule';
import { AuthModule } from './modules/auth/auth.module';
import { TenantsModule } from './modules/tenants/tenants.module';
import { UsersModule } from './modules/users/users.module';
import { AgentsModule } from './modules/agents/agents.module';
import { WorkflowsModule } from './modules/workflows/workflows.module';
import { ContactsModule } from './modules/contacts/contacts.module';
import { CallsModule } from './modules/calls/calls.module';
import { CampaignsModule } from './modules/campaigns/campaigns.module';
import { AnalyticsModule } from './modules/analytics/analytics.module';
import { KnowledgeBaseModule } from './modules/knowledge-base/knowledge-base.module';
import { IntegrationsModule } from './modules/integrations/integrations.module';
import { BillingModule } from './modules/billing/billing.module';
import { NotificationsModule } from './modules/notifications/notifications.module';
import { AdminModule } from './modules/admin/admin.module';
import { AuditModule } from './modules/audit/audit.module';
import { HealthModule } from './modules/health/health.module';
import { AbTestingModule } from './modules/ab-testing/ab-testing.module';
import { PrismaService } from './common/services/prisma.service';
import { RedisService } from './common/services/redis.service';
import { getRedisConnection } from './common/utils/parse-redis-url';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      envFilePath: ['.env.local', '.env'],
    }),
    ThrottlerModule.forRoot([
      {
        ttl: 60000,
        limit: 100,
      },
    ]),
    BullModule.forRoot({
      connection: getRedisConnection(),
    }),
    ScheduleModule.forRoot(),
    AuthModule,
    TenantsModule,
    UsersModule,
    AgentsModule,
    WorkflowsModule,
    ContactsModule,
    CallsModule,
    CampaignsModule,
    AnalyticsModule,
    KnowledgeBaseModule,
    IntegrationsModule,
    BillingModule,
    NotificationsModule,
    AdminModule,
    AuditModule,
    AbTestingModule,
    HealthModule,
  ],
  providers: [PrismaService, RedisService],
  exports: [PrismaService, RedisService],
})
export class AppModule {}
