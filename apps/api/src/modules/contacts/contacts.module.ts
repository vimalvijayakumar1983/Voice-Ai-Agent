import { Module } from '@nestjs/common';
import { ContactsController } from './contacts.controller';
import { ContactsService } from './contacts.service';
import { PrismaService } from '../../common/services/prisma.service';
import { QueueService } from '../../common/services/queue.service';

@Module({
  controllers: [ContactsController],
  providers: [ContactsService, PrismaService, QueueService],
  exports: [ContactsService],
})
export class ContactsModule {}
