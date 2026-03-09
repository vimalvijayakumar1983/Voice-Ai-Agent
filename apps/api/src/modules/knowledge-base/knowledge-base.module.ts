import { Module } from '@nestjs/common';
import { KnowledgeBaseController } from './knowledge-base.controller';
import { KnowledgeBaseService } from './knowledge-base.service';
import { PrismaService } from '../../common/services/prisma.service';
import { S3Service } from '../../common/services/s3.service';
import { QueueService } from '../../common/services/queue.service';

@Module({
  controllers: [KnowledgeBaseController],
  providers: [KnowledgeBaseService, PrismaService, S3Service, QueueService],
  exports: [KnowledgeBaseService],
})
export class KnowledgeBaseModule {}
