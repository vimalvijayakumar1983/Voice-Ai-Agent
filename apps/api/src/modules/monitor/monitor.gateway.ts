import {
  WebSocketGateway,
  WebSocketServer,
  SubscribeMessage,
  OnGatewayConnection,
  OnGatewayDisconnect,
  OnGatewayInit,
  ConnectedSocket,
  MessageBody,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { Logger } from '@nestjs/common';
import { RedisService } from '../../common/services/redis.service';
import Redis from 'ioredis';

export interface MonitorCallEvent {
  callId: string;
  tenantId: string;
  type: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface ActiveCallInfo {
  callId: string;
  tenantId: string;
  agentId: string;
  contactId?: string;
  campaignId?: string;
  phase: string;
  startedAt: string;
  toNumber?: string;
  contactName?: string;
  duration: number;
}

export interface CampaignProgress {
  campaignId: string;
  tenantId: string;
  totalContacts: number;
  contactsProcessed: number;
  activeCalls: number;
  completedCalls: number;
  failedCalls: number;
  successRate: number;
  callsPerMinute: number;
}

interface ConnectedClient {
  tenantId: string;
  userId: string;
  subscribedCalls: Set<string>;
  subscribedCampaigns: Set<string>;
}

@WebSocketGateway({
  namespace: '/monitor',
  cors: {
    origin: '*',
  },
})
export class MonitorGateway implements OnGatewayInit, OnGatewayConnection, OnGatewayDisconnect {
  @WebSocketServer()
  server: Server;

  private readonly logger = new Logger(MonitorGateway.name);
  private readonly connectedClients = new Map<string, ConnectedClient>();
  private subscriber: Redis | null = null;

  constructor(private readonly redisService: RedisService) {}

  afterInit(): void {
    this.logger.log('Monitor WebSocket gateway initialized');
    this.setupRedisSubscriber();
  }

  handleConnection(client: Socket): void {
    const tenantId = client.handshake.query.tenantId as string;
    const userId = client.handshake.query.userId as string;

    if (!tenantId || !userId) {
      this.logger.warn(`Monitor client rejected - missing tenantId or userId`);
      client.disconnect();
      return;
    }

    this.connectedClients.set(client.id, {
      tenantId,
      userId,
      subscribedCalls: new Set(),
      subscribedCampaigns: new Set(),
    });

    // Join tenant room for broadcast events
    client.join(`tenant:${tenantId}`);
    client.join(`monitor:${tenantId}`);

    this.logger.log(`Monitor client connected: ${client.id} (tenant: ${tenantId}, user: ${userId})`);

    // Send current active calls data on connection
    this.sendActiveCalls(client, tenantId);
  }

  handleDisconnect(client: Socket): void {
    this.connectedClients.delete(client.id);
    this.logger.log(`Monitor client disconnected: ${client.id}`);
  }

  // ─── CLIENT SUBSCRIPTIONS ──────────────────────────────────────────────────────

  /**
   * Subscribe to real-time updates for a specific call.
   */
  @SubscribeMessage('subscribe:call')
  handleSubscribeCall(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { callId: string },
  ): void {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    client.join(`call:${data.callId}`);
    clientInfo.subscribedCalls.add(data.callId);

    this.logger.log(`Client ${client.id} subscribed to call ${data.callId}`);
  }

  /**
   * Unsubscribe from a specific call's updates.
   */
  @SubscribeMessage('unsubscribe:call')
  handleUnsubscribeCall(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { callId: string },
  ): void {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    client.leave(`call:${data.callId}`);
    clientInfo.subscribedCalls.delete(data.callId);
  }

  /**
   * Subscribe to real-time campaign progress updates.
   */
  @SubscribeMessage('subscribe:campaign')
  handleSubscribeCampaign(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { campaignId: string },
  ): void {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    client.join(`campaign:${data.campaignId}`);
    clientInfo.subscribedCampaigns.add(data.campaignId);

    this.logger.log(`Client ${client.id} subscribed to campaign ${data.campaignId}`);
  }

  /**
   * Unsubscribe from campaign updates.
   */
  @SubscribeMessage('unsubscribe:campaign')
  handleUnsubscribeCampaign(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { campaignId: string },
  ): void {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    client.leave(`campaign:${data.campaignId}`);
    clientInfo.subscribedCampaigns.delete(data.campaignId);
  }

  /**
   * Request current active calls for the tenant.
   */
  @SubscribeMessage('request:active-calls')
  handleRequestActiveCalls(
    @ConnectedSocket() client: Socket,
  ): void {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    this.sendActiveCalls(client, clientInfo.tenantId);
  }

  /**
   * Request current campaign metrics.
   */
  @SubscribeMessage('request:campaign-metrics')
  async handleRequestCampaignMetrics(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { campaignId: string },
  ): Promise<void> {
    const clientInfo = this.connectedClients.get(client.id);
    if (!clientInfo) return;

    const metrics = await this.redisService.getJson<CampaignProgress>(
      `campaign:metrics:${data.campaignId}`,
    );

    if (metrics) {
      client.emit('campaign:metrics', {
        campaignId: data.campaignId,
        ...metrics,
      });
    }
  }

  // ─── SERVER-SIDE EMITTERS ──────────────────────────────────────────────────────

  /**
   * Emits a call started event to all monitoring clients for the tenant.
   */
  emitCallStarted(tenantId: string, callInfo: ActiveCallInfo): void {
    this.server.to(`monitor:${tenantId}`).emit('call:started', callInfo);
  }

  /**
   * Emits a live transcript update for a specific call.
   */
  emitTranscript(
    callId: string,
    tenantId: string,
    transcript: {
      text: string;
      speaker: 'ai' | 'human';
      isFinal: boolean;
      timestamp: string;
      sentiment?: string;
    },
  ): void {
    this.server.to(`call:${callId}`).emit('call:transcript', {
      callId,
      ...transcript,
    });

    // Also send to tenant monitoring room for the dashboard
    this.server.to(`monitor:${tenantId}`).emit('call:transcript', {
      callId,
      ...transcript,
    });
  }

  /**
   * Emits a call ended event.
   */
  emitCallEnded(
    callId: string,
    tenantId: string,
    summary: {
      duration: number;
      outcome: string;
      sentiment: string;
    },
  ): void {
    this.server.to(`call:${callId}`).emit('call:ended', {
      callId,
      tenantId,
      ...summary,
    });

    this.server.to(`monitor:${tenantId}`).emit('call:ended', {
      callId,
      tenantId,
      ...summary,
    });
  }

  /**
   * Emits real-time call metrics.
   */
  emitCallMetrics(
    callId: string,
    tenantId: string,
    metrics: {
      phase: string;
      duration: number;
      sttLatencyMs: number;
      llmLatencyMs: number;
      ttsLatencyMs: number;
      turnCount: number;
    },
  ): void {
    this.server.to(`call:${callId}`).emit('call:metrics', {
      callId,
      ...metrics,
    });
  }

  /**
   * Emits campaign progress updates.
   */
  emitCampaignProgress(campaignId: string, tenantId: string, progress: CampaignProgress): void {
    this.server.to(`campaign:${campaignId}`).emit('campaign:progress', progress);
    this.server.to(`monitor:${tenantId}`).emit('campaign:progress', progress);
  }

  // ─── PRIVATE METHODS ──────────────────────────────────────────────────────────

  /**
   * Sets up a Redis subscriber to listen for call events published by the
   * call orchestrator and other services.
   */
  private setupRedisSubscriber(): void {
    try {
      // Create a separate Redis connection for subscribing
      const client = this.redisService.getClient();
      this.subscriber = client.duplicate();

      this.subscriber.on('message', (channel: string, message: string) => {
        this.handleRedisMessage(channel, message);
      });

      // Subscribe to all tenant call event channels using pattern subscription
      this.subscriber.psubscribe('call:events:*', (err) => {
        if (err) {
          this.logger.error(`Failed to subscribe to call events: ${err.message}`);
        } else {
          this.logger.log('Subscribed to call:events:* Redis channel');
        }
      });

      this.subscriber.on('pmessage', (_pattern: string, channel: string, message: string) => {
        this.handleRedisMessage(channel, message);
      });

    } catch (error) {
      this.logger.error(`Failed to setup Redis subscriber: ${(error as Error).message}`);
    }
  }

  /**
   * Handles messages received from Redis pub/sub and routes them to WebSocket clients.
   */
  private handleRedisMessage(channel: string, message: string): void {
    try {
      const event: MonitorCallEvent = JSON.parse(message);
      const { callId, tenantId, type, data, timestamp } = event;

      switch (type) {
        case 'transcript':
          this.emitTranscript(callId, tenantId, {
            text: data.text as string,
            speaker: data.speaker as 'ai' | 'human',
            isFinal: data.isFinal as boolean,
            timestamp,
            sentiment: data.sentiment as string | undefined,
          });
          break;

        case 'phase_change':
          this.server.to(`call:${callId}`).emit('call:phase', {
            callId,
            phase: data.newPhase,
            previousPhase: data.previousPhase,
            timestamp,
          });
          this.server.to(`monitor:${tenantId}`).emit('call:phase', {
            callId,
            phase: data.newPhase,
            timestamp,
          });
          break;

        case 'error':
          this.server.to(`call:${callId}`).emit('call:error', {
            callId,
            error: data.error,
            stage: data.stage,
            timestamp,
          });
          break;

        default:
          // Forward generic events to subscribed clients
          this.server.to(`call:${callId}`).emit(`call:${type}`, {
            callId,
            tenantId,
            ...data,
            timestamp,
          });
      }
    } catch (error) {
      this.logger.debug(`Failed to handle Redis message: ${(error as Error).message}`);
    }
  }

  /**
   * Sends the current list of active calls to a client.
   */
  private async sendActiveCalls(client: Socket, tenantId: string): Promise<void> {
    try {
      // Scan Redis for active call sessions for this tenant
      const redisClient = this.redisService.getClient();
      const keys = await this.scanKeys(redisClient, `call:session:*`);

      const activeCalls: ActiveCallInfo[] = [];

      for (const key of keys) {
        const session = await this.redisService.getJson<{
          callId: string;
          tenantId: string;
          agentId: string;
          phase: string;
          startedAt: string;
          contactId?: string;
          campaignId?: string;
        }>(key);

        if (session && session.tenantId === tenantId) {
          const startedAt = new Date(session.startedAt);
          activeCalls.push({
            callId: session.callId,
            tenantId: session.tenantId,
            agentId: session.agentId,
            contactId: session.contactId,
            campaignId: session.campaignId,
            phase: session.phase,
            startedAt: session.startedAt,
            duration: Math.round((Date.now() - startedAt.getTime()) / 1000),
          });
        }
      }

      client.emit('active-calls', activeCalls);
    } catch (error) {
      this.logger.error(`Failed to send active calls: ${(error as Error).message}`);
    }
  }

  /**
   * Scans Redis keys matching a pattern.
   */
  private async scanKeys(client: Redis, pattern: string): Promise<string[]> {
    const keys: string[] = [];
    let cursor = '0';

    do {
      const [nextCursor, batch] = await client.scan(cursor, 'MATCH', pattern, 'COUNT', 100);
      cursor = nextCursor;
      keys.push(...batch);
    } while (cursor !== '0');

    return keys;
  }
}
