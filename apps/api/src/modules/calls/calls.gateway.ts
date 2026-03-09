import {
  WebSocketGateway,
  WebSocketServer,
  SubscribeMessage,
  OnGatewayConnection,
  OnGatewayDisconnect,
  ConnectedSocket,
  MessageBody,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { Logger } from '@nestjs/common';

export interface CallEvent {
  callId: string;
  tenantId: string;
  type: 'status_change' | 'transcript_update' | 'call_ended' | 'dtmf' | 'error';
  data: Record<string, unknown>;
  timestamp: string;
}

@WebSocketGateway({
  namespace: '/calls',
  cors: {
    origin: '*',
  },
})
export class CallsGateway implements OnGatewayConnection, OnGatewayDisconnect {
  @WebSocketServer()
  server: Server;

  private readonly logger = new Logger(CallsGateway.name);
  private readonly connectedClients = new Map<string, { tenantId: string; userId: string }>();

  handleConnection(client: Socket): void {
    const tenantId = client.handshake.query.tenantId as string;
    const userId = client.handshake.query.userId as string;

    if (!tenantId || !userId) {
      client.disconnect();
      return;
    }

    this.connectedClients.set(client.id, { tenantId, userId });
    client.join(`tenant:${tenantId}`);
    this.logger.log(`Client ${client.id} connected for tenant ${tenantId}`);
  }

  handleDisconnect(client: Socket): void {
    this.connectedClients.delete(client.id);
    this.logger.log(`Client ${client.id} disconnected`);
  }

  @SubscribeMessage('subscribe:call')
  handleSubscribeCall(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { callId: string },
  ): void {
    client.join(`call:${data.callId}`);
    this.logger.log(`Client ${client.id} subscribed to call ${data.callId}`);
  }

  @SubscribeMessage('unsubscribe:call')
  handleUnsubscribeCall(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { callId: string },
  ): void {
    client.leave(`call:${data.callId}`);
  }

  emitCallEvent(event: CallEvent): void {
    this.server.to(`call:${event.callId}`).emit('call:event', event);
    this.server.to(`tenant:${event.tenantId}`).emit('call:event', event);
  }

  emitTranscriptUpdate(
    callId: string,
    tenantId: string,
    transcript: { role: string; content: string; timestamp: string },
  ): void {
    this.emitCallEvent({
      callId,
      tenantId,
      type: 'transcript_update',
      data: transcript,
      timestamp: new Date().toISOString(),
    });
  }

  emitCallStatusChange(callId: string, tenantId: string, status: string): void {
    this.emitCallEvent({
      callId,
      tenantId,
      type: 'status_change',
      data: { status },
      timestamp: new Date().toISOString(),
    });
  }
}
