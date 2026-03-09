import { Module } from '@nestjs/common';
import { AdminController } from './admin.controller';
import { AdminService } from './admin.service';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';

@Module({
  controllers: [AdminController],
  providers: [AdminService, PrismaService, RedisService],
  exports: [AdminService],
})
export class AdminModule {}
