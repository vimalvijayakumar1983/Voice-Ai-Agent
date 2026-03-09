import { Module } from '@nestjs/common';
import { CallsController } from './calls.controller';
import { CallsService } from './calls.service';
import { CallsGateway } from './calls.gateway';
import { PrismaService } from '../../common/services/prisma.service';
import { S3Service } from '../../common/services/s3.service';

@Module({
  controllers: [CallsController],
  providers: [CallsService, CallsGateway, PrismaService, S3Service],
  exports: [CallsService, CallsGateway],
})
export class CallsModule {}
