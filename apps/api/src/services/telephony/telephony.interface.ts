export interface CallOptions {
  to: string;
  from: string;
  callbackUrl?: string;
  statusCallbackUrl?: string;
  timeout?: number;
  record?: boolean;
  metadata?: Record<string, unknown>;
}

export interface CallResult {
  callSid: string;
  status: string;
  provider: string;
}

export interface CallStatus {
  callSid: string;
  status: string;
  duration?: number;
  startTime?: Date;
  endTime?: Date;
  direction: string;
}

export interface ITelephonyProvider {
  readonly providerName: string;

  makeCall(options: CallOptions): Promise<CallResult>;

  answerCall(callSid: string, actions: Record<string, unknown>): Promise<void>;

  endCall(callSid: string): Promise<void>;

  transferCall(callSid: string, targetNumber: string): Promise<void>;

  getCallStatus(callSid: string): Promise<CallStatus>;

  sendDTMF(callSid: string, digits: string): Promise<void>;
}
