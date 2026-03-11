import { Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';
import * as http from 'http';
import * as WebSocket from 'ws';

/**
 * Vonage Audio Stream session tracking.
 * Vonage connects via raw WebSocket sending L16 PCM audio at 16kHz.
 */
export interface VonageStreamSession {
  callUuid: string;
  agentId: string;
  tenantId: string;
  ws: WebSocket.WebSocket;
  startedAt: Date;
  isActive: boolean;
}

/**
 * Handles raw WebSocket connections from Vonage Voice API.
 *
 * When a Vonage call connects, the NCCO directs it to a WebSocket endpoint.
 * Vonage sends raw L16 (16-bit signed, 16kHz, mono) PCM audio as binary frames.
 * Audio is sent back to the caller by writing raw L16 binary frames to the WebSocket.
 *
 * This is fundamentally different from Twilio's Media Streams which use JSON messages
 * with base64-encoded mulaw audio.
 */
export class VonageAudioStreamGateway extends EventEmitter {
  private readonly logger = new Logger(VonageAudioStreamGateway.name);
  private wss: WebSocket.Server | null = null;
  private readonly activeSessions = new Map<string, VonageStreamSession>();

  constructor(private readonly configService: ConfigService) {
    super();
  }

  /**
   * Attaches the WebSocket server to an existing HTTP server,
   * handling upgrade requests on the Vonage audio stream path.
   */
  attachToServer(server: http.Server): void {
    this.wss = new WebSocket.Server({ noServer: true });

    server.on('upgrade', (request: http.IncomingMessage, socket, head) => {
      const pathname = request.url?.split('?')[0];

      if (pathname === '/api/v1/telephony/vonage/audio-stream') {
        this.wss!.handleUpgrade(request, socket as any, head, (ws) => {
          this.handleConnection(ws, request);
        });
      }
      // Don't handle upgrades for other paths — let other handlers (Socket.IO, etc.) manage them
    });

    this.logger.log('Vonage audio stream WebSocket gateway attached');
  }

  /**
   * Handles a new WebSocket connection from Vonage.
   * Vonage passes custom headers from the NCCO connect action.
   */
  private handleConnection(ws: WebSocket.WebSocket, request: http.IncomingMessage): void {
    // Vonage passes custom headers from the NCCO WebSocket endpoint config
    const agentId = request.headers['agentid'] as string || '';
    const tenantId = request.headers['tenantid'] as string || '';
    const callUuid = request.headers['calluuid'] as string || '';

    this.logger.log(
      `Vonage WebSocket connected: callUuid=${callUuid}, agentId=${agentId}, tenantId=${tenantId}`,
    );

    if (!callUuid) {
      this.logger.warn('Vonage WebSocket connection without callUuid, closing');
      ws.close(1008, 'Missing callUuid');
      return;
    }

    const session: VonageStreamSession = {
      callUuid,
      agentId,
      tenantId,
      ws,
      startedAt: new Date(),
      isActive: true,
    };

    this.activeSessions.set(callUuid, session);

    // Emit session start for the call orchestrator
    this.emit('stream:start', {
      callUuid,
      agentId,
      tenantId,
      provider: 'vonage',
    });

    ws.on('message', (data: WebSocket.Data, isBinary: boolean) => {
      if (!session.isActive) return;

      if (isBinary && Buffer.isBuffer(data)) {
        // Vonage sends raw L16 PCM audio (16-bit signed LE, 16kHz, mono)
        // This is already in the format our pipeline expects
        this.emit('stream:audio', {
          callUuid,
          agentId,
          tenantId,
          pcmAudio: data,
          provider: 'vonage',
        });
      } else {
        // Vonage may send a JSON text frame on initial connect with metadata
        try {
          const textData = typeof data === 'string' ? data : data.toString('utf-8');
          const metadata = JSON.parse(textData);
          this.logger.debug(`Vonage WebSocket metadata for ${callUuid}: ${JSON.stringify(metadata)}`);
          this.emit('stream:metadata', {
            callUuid,
            metadata,
            provider: 'vonage',
          });
        } catch {
          this.logger.debug(`Vonage WebSocket non-JSON text frame for ${callUuid}`);
        }
      }
    });

    ws.on('close', (code: number, reason: Buffer) => {
      this.logger.log(
        `Vonage WebSocket closed: callUuid=${callUuid}, code=${code}, reason=${reason?.toString() || 'none'}`,
      );
      session.isActive = false;
      this.activeSessions.delete(callUuid);

      this.emit('stream:stop', {
        callUuid,
        agentId,
        tenantId,
        provider: 'vonage',
      });
    });

    ws.on('error', (error: Error) => {
      this.logger.error(`Vonage WebSocket error for ${callUuid}: ${error.message}`);
      session.isActive = false;
    });
  }

  /**
   * Sends L16 PCM audio back to Vonage over the WebSocket.
   * Vonage expects raw 16-bit signed LE PCM at 16kHz.
   */
  sendAudio(callUuid: string, pcmAudio: Buffer): boolean {
    const session = this.activeSessions.get(callUuid);
    if (!session?.isActive || session.ws.readyState !== WebSocket.default.OPEN) {
      return false;
    }

    try {
      session.ws.send(pcmAudio, { binary: true });
      return true;
    } catch (error) {
      this.logger.error(`Failed to send audio to Vonage ${callUuid}: ${(error as Error).message}`);
      return false;
    }
  }

  /**
   * Closes the WebSocket connection for a specific call (e.g., on hangup).
   */
  closeConnection(callUuid: string): void {
    const session = this.activeSessions.get(callUuid);
    if (session) {
      session.isActive = false;
      session.ws.close(1000, 'Call ended');
      this.activeSessions.delete(callUuid);
      this.logger.log(`Vonage WebSocket closed for call ${callUuid}`);
    }
  }

  /**
   * Returns the session for a given callUuid.
   */
  getSession(callUuid: string): VonageStreamSession | undefined {
    return this.activeSessions.get(callUuid);
  }

  /**
   * Returns the count of active Vonage stream sessions.
   */
  getActiveSessionCount(): number {
    return this.activeSessions.size;
  }
}
