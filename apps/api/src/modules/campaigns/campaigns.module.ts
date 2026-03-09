import { Module } from '@nestjs/common';
import { CampaignsController } from './campaigns.controller';
import { CampaignsService } from './campaigns.service';
import { PrismaService } from '../../common/services/prisma.service';
import { QueueService } from '../../common/services/queue.service';

@Module({
  controllers: [CampaignsController],
  providers: [CampaignsService, PrismaService, QueueService],
  exports: [CampaignsService],
})
export class CampaignsModule {}
