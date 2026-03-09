import { Module } from '@nestjs/common';
import { WorkflowsController } from './workflows.controller';
import { WorkflowsService } from './workflows.service';
import { PrismaService } from '../../common/services/prisma.service';

@Module({
  controllers: [WorkflowsController],
  providers: [WorkflowsService, PrismaService],
  exports: [WorkflowsService],
})
export class WorkflowsModule {}
