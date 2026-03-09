import { Module } from '@nestjs/common';
import { IntegrationsController } from './integrations.controller';
import { IntegrationsService } from './integrations.service';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';

@Module({
  controllers: [IntegrationsController],
  providers: [IntegrationsService, PrismaService, RedisService],
  exports: [IntegrationsService],
})
export class IntegrationsModule {}
