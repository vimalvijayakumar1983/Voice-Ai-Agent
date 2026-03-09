import {
  WebSocketGateway,
  WebSocketServer,
  OnGatewayConnection,
  OnGatewayDisconnect,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { Logger } from '@nestjs/common';

@WebSocketGateway({
  namespace: '/notifications',
  cors: {
    origin: '*',
  },
})
export class NotificationsGateway implements OnGatewayConnection, OnGatewayDisconnect {
  @WebSocketServer()
  server: Server;

  private readonly logger = new Logger(NotificationsGateway.name);

  handleConnection(client: Socket): void {
    const userId = client.handshake.query.userId as string;
    const tenantId = client.handshake.query.tenantId as string;

    if (!userId || !tenantId) {
      client.disconnect();
      return;
    }

    client.join(`user:${userId}`);
    client.join(`tenant:${tenantId}`);
    this.logger.log(`Notification client connected: user ${userId}`);
  }

  handleDisconnect(client: Socket): void {
    this.logger.log(`Notification client disconnected: ${client.id}`);
  }

  sendToUser(userId: string, notification: Record<string, unknown>): void {
    this.server.to(`user:${userId}`).emit('notification', notification);
  }

  sendToTenant(tenantId: string, notification: Record<string, unknown>): void {
    this.server.to(`tenant:${tenantId}`).emit('notification', notification);
  }

  broadcastToAll(notification: Record<string, unknown>): void {
    this.server.emit('notification', notification);
  }
}
