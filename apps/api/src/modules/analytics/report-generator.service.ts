import { Injectable, Logger, NotFoundException } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { QueueService } from '../../common/services/queue.service';
import { S3Service } from '../../common/services/s3.service';
import { AnalyticsAggregatorService } from './analytics-aggregator.service';

// ============================================================================
// Types
// ============================================================================

export enum ReportType {
  CALL_SUMMARY = 'CALL_SUMMARY',
  CAMPAIGN_PERFORMANCE = 'CAMPAIGN_PERFORMANCE',
  AGENT_SCORECARD = 'AGENT_SCORECARD',
  COMPLIANCE_AUDIT = 'COMPLIANCE_AUDIT',
}

export enum ReportFormat {
  PDF = 'PDF',
  CSV = 'CSV',
  JSON = 'JSON',
}

export enum ReportStatus {
  QUEUED = 'QUEUED',
  GENERATING = 'GENERATING',
  COMPLETED = 'COMPLETED',
  FAILED = 'FAILED',
}

export interface ReportRequest {
  type: ReportType;
  format: ReportFormat;
  dateFrom: string;
  dateTo: string;
  filters?: Record<string, unknown>;
  emailTo?: string;
}

export interface ReportRecord {
  id: string;
  tenantId: string;
  type: ReportType;
  format: ReportFormat;
  status: ReportStatus;
  dateFrom: string;
  dateTo: string;
  filters: Record<string, unknown>;
  storageKey?: string;
  downloadUrl?: string;
  fileSize?: number;
  error?: string;
  requestedBy: string;
  emailTo?: string;
  createdAt: string;
  completedAt?: string;
}

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class ReportGeneratorService {
  private readonly logger = new Logger(ReportGeneratorService.name);
  private readonly REPORT_PREFIX = 'report:';
  private readonly REPORT_TTL = 86400 * 7; // 7 days

  constructor(
    private readonly prisma: PrismaService,
    private readonly redis: RedisService,
    private readonly queue: QueueService,
    private readonly s3: S3Service,
    private readonly aggregator: AnalyticsAggregatorService,
  ) {}

  // --------------------------------------------------------------------------
  // Report Lifecycle
  // --------------------------------------------------------------------------

  async requestReport(
    tenantId: string,
    userId: string,
    request: ReportRequest,
  ): Promise<ReportRecord> {
    const reportId = `rpt_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;

    const record: ReportRecord = {
      id: reportId,
      tenantId,
      type: request.type,
      format: request.format,
      status: ReportStatus.QUEUED,
      dateFrom: request.dateFrom,
      dateTo: request.dateTo,
      filters: request.filters || {},
      requestedBy: userId,
      emailTo: request.emailTo,
      createdAt: new Date().toISOString(),
    };

    await this.redis.setJson(`${this.REPORT_PREFIX}${reportId}`, record, this.REPORT_TTL);

    // Also store in a tenant-scoped index
    const indexKey = `${this.REPORT_PREFIX}index:${tenantId}`;
    const index = (await this.redis.getJson<string[]>(indexKey)) || [];
    index.unshift(reportId);
    // Keep only last 100 reports in index
    await this.redis.setJson(indexKey, index.slice(0, 100), this.REPORT_TTL);

    // Queue the generation job
    await this.queue.addJob('report-generation', 'generate-report', {
      tenantId,
      reportId,
      type: request.type,
      format: request.format,
      dateFrom: request.dateFrom,
      dateTo: request.dateTo,
      filters: request.filters || {},
      emailTo: request.emailTo,
    });

    this.logger.log(
      `Queued report ${reportId} (${request.type}/${request.format}) for tenant ${tenantId}`,
    );

    return record;
  }

  async getReport(reportId: string): Promise<ReportRecord | null> {
    return this.redis.getJson<ReportRecord>(`${this.REPORT_PREFIX}${reportId}`);
  }

  async getReportsByTenant(tenantId: string): Promise<ReportRecord[]> {
    const indexKey = `${this.REPORT_PREFIX}index:${tenantId}`;
    const index = (await this.redis.getJson<string[]>(indexKey)) || [];

    const reports: ReportRecord[] = [];
    for (const reportId of index) {
      const report = await this.redis.getJson<ReportRecord>(
        `${this.REPORT_PREFIX}${reportId}`,
      );
      if (report) reports.push(report);
    }

    return reports;
  }

  async getDownloadUrl(reportId: string, tenantId: string): Promise<string> {
    const report = await this.getReport(reportId);
    if (!report) throw new NotFoundException(`Report ${reportId} not found`);
    if (report.tenantId !== tenantId) {
      throw new NotFoundException(`Report ${reportId} not found`);
    }
    if (report.status !== ReportStatus.COMPLETED || !report.storageKey) {
      throw new NotFoundException(`Report ${reportId} is not ready for download`);
    }

    return this.s3.getSignedDownloadUrl(report.storageKey, 3600);
  }

  // --------------------------------------------------------------------------
  // Report Generation (called by queue worker)
  // --------------------------------------------------------------------------

  async generateReport(reportId: string): Promise<void> {
    const record = await this.getReport(reportId);
    if (!record) {
      this.logger.error(`Report ${reportId} not found`);
      return;
    }

    record.status = ReportStatus.GENERATING;
    await this.redis.setJson(`${this.REPORT_PREFIX}${reportId}`, record, this.REPORT_TTL);

    try {
      let content: Buffer;

      switch (record.type) {
        case ReportType.CALL_SUMMARY:
          content = await this.generateCallSummaryReport(record);
          break;
        case ReportType.CAMPAIGN_PERFORMANCE:
          content = await this.generateCampaignPerformanceReport(record);
          break;
        case ReportType.AGENT_SCORECARD:
          content = await this.generateAgentScorecardReport(record);
          break;
        case ReportType.COMPLIANCE_AUDIT:
          content = await this.generateComplianceAuditReport(record);
          break;
        default:
          throw new Error(`Unknown report type: ${record.type}`);
      }

      // Upload to S3
      const ext = record.format === ReportFormat.CSV ? 'csv' : record.format === ReportFormat.JSON ? 'json' : 'html';
      const contentType =
        record.format === ReportFormat.CSV
          ? 'text/csv'
          : record.format === ReportFormat.JSON
            ? 'application/json'
            : 'text/html';

      const storageKey = this.s3.buildKey(
        record.tenantId,
        'reports',
        `${record.type.toLowerCase()}_${record.id}.${ext}`,
      );

      const uploadResult = await this.s3.upload(storageKey, content, contentType, {
        reportId: record.id,
        reportType: record.type,
        tenantId: record.tenantId,
      });

      record.status = ReportStatus.COMPLETED;
      record.storageKey = storageKey;
      record.downloadUrl = uploadResult.url;
      record.fileSize = content.length;
      record.completedAt = new Date().toISOString();

      await this.redis.setJson(`${this.REPORT_PREFIX}${reportId}`, record, this.REPORT_TTL);

      // Send email notification if requested
      if (record.emailTo) {
        await this.queue.addJob('notifications', 'send-email', {
          tenantId: record.tenantId,
          to: record.emailTo,
          subject: `Report Ready: ${record.type}`,
          body: `Your ${record.type} report is ready for download.`,
          reportId: record.id,
        });
      }

      this.logger.log(`Report ${reportId} generated successfully (${content.length} bytes)`);
    } catch (error) {
      this.logger.error(`Report ${reportId} generation failed: ${error}`);
      record.status = ReportStatus.FAILED;
      record.error = String(error);
      await this.redis.setJson(`${this.REPORT_PREFIX}${reportId}`, record, this.REPORT_TTL);
    }
  }

  // --------------------------------------------------------------------------
  // Report Generators
  // --------------------------------------------------------------------------

  private async generateCallSummaryReport(record: ReportRecord): Promise<Buffer> {
    const from = new Date(record.dateFrom);
    const to = new Date(record.dateTo);
    const tenantId = record.tenantId;

    const metrics = await this.aggregator.aggregateRange(tenantId, from, to, 'daily');

    const calls = await this.prisma.call.findMany({
      where: {
        tenantId,
        createdAt: { gte: from, lte: to },
      },
      select: {
        id: true,
        status: true,
        direction: true,
        duration: true,
        sentiment: true,
        outcome: true,
        createdAt: true,
        phoneTo: true,
        phoneFrom: true,
        agent: { select: { name: true } },
        campaign: { select: { name: true } },
        contact: { select: { firstName: true, lastName: true } },
      },
      orderBy: { createdAt: 'desc' },
      take: 5000,
    });

    if (record.format === ReportFormat.CSV) {
      return this.generateCsv(
        [
          'Call ID',
          'Date',
          'Direction',
          'Status',
          'Duration (s)',
          'Sentiment',
          'Outcome',
          'Agent',
          'Campaign',
          'Contact',
          'From',
          'To',
        ],
        calls.map((c) => [
          c.id,
          new Date(c.createdAt).toISOString(),
          c.direction,
          c.status,
          String(c.duration || 0),
          c.sentiment || '',
          c.outcome || '',
          c.agent?.name || '',
          c.campaign?.name || '',
          [c.contact?.firstName, c.contact?.lastName].filter(Boolean).join(' ') || '',
          c.phoneFrom || '',
          c.phoneTo || '',
        ]),
      );
    }

    if (record.format === ReportFormat.JSON) {
      return Buffer.from(
        JSON.stringify({ metrics, calls, generatedAt: new Date().toISOString() }, null, 2),
      );
    }

    // HTML/PDF format
    return this.generateHtmlReport('Call Summary Report', record, [
      { title: 'Overview', content: this.metricsToHtml(metrics as unknown as Record<string, unknown>) },
      {
        title: 'Call Details',
        content: this.tableToHtml(
          ['Date', 'Direction', 'Status', 'Duration', 'Sentiment', 'Agent', 'Contact'],
          calls.slice(0, 500).map((c) => [
            new Date(c.createdAt).toLocaleDateString(),
            c.direction,
            c.status,
            `${c.duration || 0}s`,
            c.sentiment || '-',
            c.agent?.name || '-',
            [c.contact?.firstName, c.contact?.lastName].filter(Boolean).join(' ') || '-',
          ]),
        ),
      },
    ]);
  }

  private async generateCampaignPerformanceReport(record: ReportRecord): Promise<Buffer> {
    const from = new Date(record.dateFrom);
    const to = new Date(record.dateTo);
    const tenantId = record.tenantId;

    const campaigns = await this.prisma.campaign.findMany({
      where: {
        tenantId,
        createdAt: { lte: to },
      },
      select: {
        id: true,
        name: true,
        status: true,
        startedAt: true,
        completedAt: true,
        metrics: true,
        _count: { select: { calls: true } },
      },
      orderBy: { createdAt: 'desc' },
    });

    const campaignData = await Promise.all(
      campaigns.map(async (c) => {
        const funnel = await this.aggregator.getCampaignFunnel(tenantId, c.id);
        return { ...c, funnel };
      }),
    );

    if (record.format === ReportFormat.CSV) {
      return this.generateCsv(
        [
          'Campaign',
          'Status',
          'Total Calls',
          'Contacted',
          'Answered',
          'Completed',
          'Converted',
          'Answer Rate',
          'Conversion Rate',
        ],
        campaignData.map((c) => [
          c.name,
          c.status,
          String(c._count.calls),
          String(c.funnel.called),
          String(c.funnel.answered),
          String(c.funnel.completed),
          String(c.funnel.converted),
          `${c.funnel.conversionRates.answerRate.toFixed(1)}%`,
          `${c.funnel.conversionRates.conversionRate.toFixed(1)}%`,
        ]),
      );
    }

    if (record.format === ReportFormat.JSON) {
      return Buffer.from(
        JSON.stringify({ campaigns: campaignData, generatedAt: new Date().toISOString() }, null, 2),
      );
    }

    return this.generateHtmlReport('Campaign Performance Report', record, [
      {
        title: 'Campaign Overview',
        content: this.tableToHtml(
          ['Campaign', 'Status', 'Calls', 'Answer Rate', 'Completion Rate', 'Conversion Rate'],
          campaignData.map((c) => [
            c.name,
            c.status,
            String(c._count.calls),
            `${c.funnel.conversionRates.answerRate.toFixed(1)}%`,
            `${c.funnel.conversionRates.completionRate.toFixed(1)}%`,
            `${c.funnel.conversionRates.conversionRate.toFixed(1)}%`,
          ]),
        ),
      },
    ]);
  }

  private async generateAgentScorecardReport(record: ReportRecord): Promise<Buffer> {
    const from = new Date(record.dateFrom);
    const to = new Date(record.dateTo);
    const tenantId = record.tenantId;

    const scores = await this.aggregator.getAgentPerformanceScores(tenantId, from, to);

    if (record.format === ReportFormat.CSV) {
      return this.generateCsv(
        [
          'Rank',
          'Agent',
          'Score',
          'Total Calls',
          'Completed',
          'Success Rate',
          'Avg Duration (s)',
          'Avg Sentiment',
          'Trend',
        ],
        scores.map((s) => [
          String(s.rank),
          s.agentName,
          s.score.toFixed(1),
          String(s.totalCalls),
          String(s.completedCalls),
          `${s.successRate.toFixed(1)}%`,
          s.avgDuration.toFixed(0),
          s.avgSentiment.toFixed(2),
          `${s.trend >= 0 ? '+' : ''}${s.trend.toFixed(1)}%`,
        ]),
      );
    }

    if (record.format === ReportFormat.JSON) {
      return Buffer.from(
        JSON.stringify({ agents: scores, generatedAt: new Date().toISOString() }, null, 2),
      );
    }

    return this.generateHtmlReport('Agent Scorecard Report', record, [
      {
        title: 'Agent Performance Rankings',
        content: this.tableToHtml(
          ['Rank', 'Agent', 'Score', 'Calls', 'Success Rate', 'Avg Duration', 'Sentiment', 'Trend'],
          scores.map((s) => [
            `#${s.rank}`,
            s.agentName,
            s.score.toFixed(1),
            String(s.totalCalls),
            `${s.successRate.toFixed(1)}%`,
            `${s.avgDuration.toFixed(0)}s`,
            s.avgSentiment.toFixed(2),
            `${s.trend >= 0 ? '+' : ''}${s.trend.toFixed(1)}%`,
          ]),
        ),
      },
    ]);
  }

  private async generateComplianceAuditReport(record: ReportRecord): Promise<Buffer> {
    const from = new Date(record.dateFrom);
    const to = new Date(record.dateTo);
    const tenantId = record.tenantId;

    // Get calls with compliance data
    const calls = await this.prisma.call.findMany({
      where: {
        tenantId,
        createdAt: { gte: from, lte: to },
      },
      select: {
        id: true,
        status: true,
        complianceChecklist: true,
        recordingUrl: true,
        createdAt: true,
        agent: { select: { name: true } },
        contact: { select: { firstName: true, lastName: true, consentStatus: true } },
      },
      orderBy: { createdAt: 'desc' },
      take: 5000,
    });

    // DNC violations
    const dncContacts = await this.prisma.contact.count({
      where: { tenantId, dncStatus: true },
    });

    const dncCallCount = await this.prisma.call.count({
      where: {
        tenantId,
        createdAt: { gte: from, lte: to },
        contact: { dncStatus: true },
      },
    });

    // Consent stats
    const consentBreakdown = await this.prisma.contact.groupBy({
      by: ['consentStatus'],
      where: { tenantId },
      _count: { id: true },
    });

    // Recording availability
    const withRecording = calls.filter((c) => c.recordingUrl).length;
    const withoutRecording = calls.length - withRecording;

    const complianceData = {
      totalCallsAudited: calls.length,
      dncContacts,
      dncCallAttempts: dncCallCount,
      consentBreakdown: consentBreakdown.reduce(
        (acc, c) => ({ ...acc, [c.consentStatus]: c._count.id }),
        {} as Record<string, number>,
      ),
      recordingCoverage: {
        withRecording,
        withoutRecording,
        percentage: calls.length > 0 ? (withRecording / calls.length) * 100 : 0,
      },
    };

    if (record.format === ReportFormat.CSV) {
      return this.generateCsv(
        ['Call ID', 'Date', 'Agent', 'Contact', 'Status', 'Consent', 'Has Recording', 'Compliance Check'],
        calls.map((c) => {
          const checklist = c.complianceChecklist as Record<string, boolean> | null;
          const allPassed = checklist
            ? Object.values(checklist).every((v) => v === true)
            : false;
          return [
            c.id,
            new Date(c.createdAt).toISOString(),
            c.agent?.name || '',
            [c.contact?.firstName, c.contact?.lastName].filter(Boolean).join(' ') || '',
            c.status,
            c.contact?.consentStatus || 'UNKNOWN',
            c.recordingUrl ? 'Yes' : 'No',
            allPassed ? 'PASSED' : checklist ? 'FAILED' : 'N/A',
          ];
        }),
      );
    }

    if (record.format === ReportFormat.JSON) {
      return Buffer.from(
        JSON.stringify(
          { compliance: complianceData, calls: calls.slice(0, 500), generatedAt: new Date().toISOString() },
          null,
          2,
        ),
      );
    }

    return this.generateHtmlReport('Compliance Audit Report', record, [
      {
        title: 'Compliance Summary',
        content: `
          <div class="grid">
            <div class="metric"><span class="label">Total Calls Audited</span><span class="value">${complianceData.totalCallsAudited}</span></div>
            <div class="metric"><span class="label">DNC Contacts</span><span class="value">${complianceData.dncContacts}</span></div>
            <div class="metric"><span class="label">DNC Call Attempts</span><span class="value" style="color: ${dncCallCount > 0 ? '#ef4444' : '#22c55e'}">${complianceData.dncCallAttempts}</span></div>
            <div class="metric"><span class="label">Recording Coverage</span><span class="value">${complianceData.recordingCoverage.percentage.toFixed(1)}%</span></div>
          </div>
        `,
      },
      {
        title: 'Consent Status Distribution',
        content: this.tableToHtml(
          ['Status', 'Count'],
          Object.entries(complianceData.consentBreakdown).map(([k, v]) => [k, String(v)]),
        ),
      },
    ]);
  }

  // --------------------------------------------------------------------------
  // Format Helpers
  // --------------------------------------------------------------------------

  private generateCsv(headers: string[], rows: string[][]): Buffer {
    const escapeCsv = (val: string) => {
      if (val.includes(',') || val.includes('"') || val.includes('\n')) {
        return `"${val.replace(/"/g, '""')}"`;
      }
      return val;
    };

    const lines = [
      headers.map(escapeCsv).join(','),
      ...rows.map((row) => row.map(escapeCsv).join(',')),
    ];

    return Buffer.from(lines.join('\n'), 'utf-8');
  }

  private generateHtmlReport(
    title: string,
    record: ReportRecord,
    sections: Array<{ title: string; content: string }>,
  ): Buffer {
    const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>${title}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; color: #1a1a1a; }
    h1 { color: #0f172a; border-bottom: 2px solid #3b82f6; padding-bottom: 12px; }
    h2 { color: #1e293b; margin-top: 32px; }
    .meta { color: #64748b; font-size: 14px; margin-bottom: 24px; }
    table { width: 100%; border-collapse: collapse; margin: 16px 0; }
    th { background: #f1f5f9; color: #475569; font-weight: 600; text-align: left; padding: 10px 12px; border-bottom: 2px solid #e2e8f0; }
    td { padding: 8px 12px; border-bottom: 1px solid #e2e8f0; }
    tr:hover { background: #f8fafc; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 16px 0; }
    .metric { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }
    .metric .label { display: block; color: #64748b; font-size: 13px; margin-bottom: 4px; }
    .metric .value { display: block; font-size: 24px; font-weight: 700; color: #0f172a; }
    .footer { margin-top: 40px; color: #94a3b8; font-size: 12px; border-top: 1px solid #e2e8f0; padding-top: 12px; }
  </style>
</head>
<body>
  <h1>${title}</h1>
  <div class="meta">
    Period: ${new Date(record.dateFrom).toLocaleDateString()} - ${new Date(record.dateTo).toLocaleDateString()}
    | Generated: ${new Date().toLocaleString()}
  </div>
  ${sections.map((s) => `<h2>${s.title}</h2>${s.content}`).join('\n')}
  <div class="footer">Voice AI Platform - Confidential Report</div>
</body>
</html>`;

    return Buffer.from(html, 'utf-8');
  }

  private metricsToHtml(metrics: Record<string, unknown>): string {
    const cv = metrics.callVolume as Record<string, unknown>;
    const sr = metrics.successRates as Record<string, unknown>;
    const dur = metrics.duration as Record<string, unknown>;

    return `
      <div class="grid">
        <div class="metric"><span class="label">Total Calls</span><span class="value">${cv?.total || 0}</span></div>
        <div class="metric"><span class="label">Completed</span><span class="value">${cv?.completed || 0}</span></div>
        <div class="metric"><span class="label">Success Rate</span><span class="value">${((sr?.overall as number) || 0).toFixed(1)}%</span></div>
        <div class="metric"><span class="label">Avg Duration</span><span class="value">${((dur?.average as number) || 0).toFixed(0)}s</span></div>
        <div class="metric"><span class="label">Failed</span><span class="value">${cv?.failed || 0}</span></div>
        <div class="metric"><span class="label">No Answer</span><span class="value">${cv?.noAnswer || 0}</span></div>
      </div>
    `;
  }

  private tableToHtml(headers: string[], rows: string[][]): string {
    return `
      <table>
        <thead>
          <tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr>
        </thead>
        <tbody>
          ${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('\n')}
        </tbody>
      </table>
    `;
  }
}
