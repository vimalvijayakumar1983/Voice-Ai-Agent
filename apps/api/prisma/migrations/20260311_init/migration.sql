-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "vector";

-- CreateEnum
CREATE TYPE "TenantStatus" AS ENUM ('ACTIVE', 'SUSPENDED', 'TRIAL', 'CANCELLED');

-- CreateEnum
CREATE TYPE "TenantPlan" AS ENUM ('FREE', 'STARTER', 'PROFESSIONAL', 'ENTERPRISE', 'CUSTOM');

-- CreateEnum
CREATE TYPE "UserRole" AS ENUM ('SUPER_ADMIN', 'TENANT_OWNER', 'ADMIN', 'MANAGER', 'SUPERVISOR', 'QA_AUDITOR', 'AGENT_OPERATOR', 'READONLY_ANALYST');

-- CreateEnum
CREATE TYPE "MembershipRole" AS ENUM ('OWNER', 'ADMIN', 'MANAGER', 'MEMBER', 'VIEWER');

-- CreateEnum
CREATE TYPE "AgentType" AS ENUM ('INBOUND', 'OUTBOUND', 'HYBRID', 'IVR', 'SURVEY', 'APPOINTMENT', 'COLLECTIONS', 'SUPPORT');

-- CreateEnum
CREATE TYPE "Industry" AS ENUM ('HEALTHCARE', 'FINANCE', 'INSURANCE', 'REAL_ESTATE', 'RETAIL', 'ECOMMERCE', 'HOSPITALITY', 'EDUCATION', 'TECHNOLOGY', 'AUTOMOTIVE', 'LEGAL', 'GOVERNMENT', 'TELECOMMUNICATIONS', 'ENERGY', 'LOGISTICS', 'OTHER');

-- CreateEnum
CREATE TYPE "ConsentStatus" AS ENUM ('OPTED_IN', 'OPTED_OUT', 'PENDING', 'UNKNOWN');

-- CreateEnum
CREATE TYPE "CampaignStatus" AS ENUM ('DRAFT', 'SCHEDULED', 'RUNNING', 'PAUSED', 'COMPLETED', 'CANCELLED', 'FAILED');

-- CreateEnum
CREATE TYPE "CallDirection" AS ENUM ('INBOUND', 'OUTBOUND');

-- CreateEnum
CREATE TYPE "CallStatus" AS ENUM ('QUEUED', 'RINGING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'NO_ANSWER', 'BUSY', 'VOICEMAIL', 'CANCELLED', 'TRANSFERRED');

-- CreateEnum
CREATE TYPE "DocumentStatus" AS ENUM ('PENDING', 'PROCESSING', 'READY', 'FAILED', 'ARCHIVED');

-- CreateEnum
CREATE TYPE "IntegrationStatus" AS ENUM ('RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED');

-- CreateEnum
CREATE TYPE "UsageType" AS ENUM ('CALL_MINUTES', 'API_CALLS', 'TRANSCRIPTION_MINUTES', 'TTS_CHARACTERS', 'LLM_TOKENS', 'STORAGE_GB', 'SMS_SENT', 'PHONE_NUMBERS');

-- CreateEnum
CREATE TYPE "NotificationChannel" AS ENUM ('IN_APP', 'EMAIL', 'SMS', 'WEBHOOK', 'SLACK');

-- CreateEnum
CREATE TYPE "NotificationStatus" AS ENUM ('PENDING', 'SENT', 'DELIVERED', 'FAILED', 'READ');

-- CreateEnum
CREATE TYPE "ExperimentStatus" AS ENUM ('DRAFT', 'RUNNING', 'PAUSED', 'COMPLETED', 'CANCELLED');

-- CreateEnum
CREATE TYPE "SyncDirection" AS ENUM ('INBOUND', 'OUTBOUND', 'BIDIRECTIONAL');

-- CreateEnum
CREATE TYPE "SyncStatus" AS ENUM ('IDLE', 'SYNCING', 'SUCCESS', 'FAILED');

-- CreateEnum
CREATE TYPE "ReportStatus" AS ENUM ('PENDING', 'GENERATING', 'READY', 'FAILED', 'EXPIRED');

-- CreateEnum
CREATE TYPE "ReportType" AS ENUM ('CALL_SUMMARY', 'CAMPAIGN_PERFORMANCE', 'AGENT_SCORECARD', 'COMPLIANCE_AUDIT', 'USAGE_REPORT', 'COST_ANALYSIS');

-- CreateEnum
CREATE TYPE "ChunkStrategy" AS ENUM ('FIXED_SIZE', 'SENTENCE', 'SEMANTIC', 'PARAGRAPH');

-- CreateTable
CREATE TABLE "tenants" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "name" TEXT NOT NULL,
    "slug" TEXT NOT NULL,
    "industry" TEXT,
    "description" TEXT,
    "settings" JSONB NOT NULL DEFAULT '{}',
    "branding" JSONB NOT NULL DEFAULT '{}',
    "features" JSONB NOT NULL DEFAULT '{}',
    "status" "TenantStatus" NOT NULL DEFAULT 'TRIAL',
    "plan" "TenantPlan" NOT NULL DEFAULT 'FREE',
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "tenant_metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "tenants_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "users" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "email" TEXT NOT NULL,
    "firstName" TEXT NOT NULL,
    "lastName" TEXT NOT NULL,
    "passwordHash" TEXT NOT NULL,
    "phone" TEXT,
    "role" "UserRole" NOT NULL DEFAULT 'READONLY_ANALYST',
    "tenantId" UUID NOT NULL,
    "department" TEXT,
    "avatar" TEXT,
    "lastLoginAt" TIMESTAMP(3),
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "memberships" (
    "userId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "role" "MembershipRole" NOT NULL DEFAULT 'MEMBER',
    "permissions" JSONB NOT NULL DEFAULT '{}',
    "department" TEXT,

    CONSTRAINT "memberships_pkey" PRIMARY KEY ("userId","tenantId")
);

-- CreateTable
CREATE TABLE "agents" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "type" "AgentType" NOT NULL DEFAULT 'OUTBOUND',
    "industry" "Industry" NOT NULL DEFAULT 'OTHER',
    "voiceId" TEXT,
    "language" TEXT NOT NULL DEFAULT 'en-US',
    "personality" JSONB NOT NULL DEFAULT '{}',
    "toneSettings" JSONB NOT NULL DEFAULT '{}',
    "systemPrompt" TEXT,
    "knowledgeBaseId" UUID,
    "escalationRules" JSONB NOT NULL DEFAULT '[]',
    "complianceBlocks" JSONB NOT NULL DEFAULT '[]',
    "callObjective" TEXT,
    "successCriteria" TEXT,
    "endCallConditions" JSONB NOT NULL DEFAULT '[]',
    "humanTransferTriggers" JSONB NOT NULL DEFAULT '[]',
    "retryLogic" JSONB NOT NULL DEFAULT '{}',
    "timeRules" JSONB NOT NULL DEFAULT '{}',
    "config" JSONB NOT NULL DEFAULT '{}',
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdById" UUID,
    "version" INTEGER NOT NULL DEFAULT 1,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "agents_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "agent_versions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "agentId" UUID NOT NULL,
    "tenantId" UUID,
    "version" INTEGER NOT NULL,
    "config" JSONB NOT NULL,
    "createdBy" UUID,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "agent_versions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "workflows" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "agentId" UUID,
    "nodes" JSONB NOT NULL DEFAULT '[]',
    "edges" JSONB NOT NULL DEFAULT '[]',
    "variables" JSONB NOT NULL DEFAULT '{}',
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "version" INTEGER NOT NULL DEFAULT 1,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdById" UUID,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "workflows_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "workflow_versions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "workflowId" UUID NOT NULL,
    "tenantId" UUID,
    "version" INTEGER NOT NULL,
    "definition" JSONB NOT NULL,
    "config" JSONB,
    "createdBy" UUID,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "workflow_versions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "contacts" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "firstName" TEXT,
    "lastName" TEXT,
    "email" TEXT,
    "phone" TEXT,
    "company" TEXT,
    "title" TEXT,
    "tags" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "segment" TEXT,
    "consentStatus" "ConsentStatus" NOT NULL DEFAULT 'UNKNOWN',
    "dncStatus" BOOLEAN NOT NULL DEFAULT false,
    "languagePreference" TEXT,
    "preferredCallTime" TEXT,
    "timezone" TEXT,
    "customFields" JSONB NOT NULL DEFAULT '{}',
    "externalId" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "doNotContact" BOOLEAN NOT NULL DEFAULT false,
    "source" TEXT,
    "accountId" UUID,
    "lastContactedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "contacts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "accounts" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "industry" TEXT,
    "website" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "accounts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "contact_notes" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "contactId" UUID NOT NULL,
    "content" TEXT NOT NULL,
    "createdBy" UUID,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "contact_notes_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "campaigns" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "type" TEXT,
    "status" "CampaignStatus" NOT NULL DEFAULT 'DRAFT',
    "agentId" UUID NOT NULL,
    "workflowId" UUID,
    "contactListFilter" JSONB NOT NULL DEFAULT '{}',
    "contactListIds" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "retryPolicy" JSONB NOT NULL DEFAULT '{}',
    "callWindowRules" JSONB NOT NULL DEFAULT '{}',
    "callingHours" JSONB,
    "maxAttempts" INTEGER NOT NULL DEFAULT 3,
    "maxConcurrentCalls" INTEGER,
    "callsPerHour" INTEGER,
    "maxRetries" INTEGER,
    "retryDelayMinutes" INTEGER,
    "concurrency" INTEGER NOT NULL DEFAULT 1,
    "priority" INTEGER NOT NULL DEFAULT 0,
    "scheduledAt" TIMESTAMP(3),
    "scheduledStartAt" TIMESTAMP(3),
    "startedAt" TIMESTAMP(3),
    "completedAt" TIMESTAMP(3),
    "metrics" JSONB NOT NULL DEFAULT '{}',
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdBy" UUID,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "campaigns_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "calls" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "campaignId" UUID,
    "agentId" UUID NOT NULL,
    "contactId" UUID,
    "direction" "CallDirection" NOT NULL,
    "status" "CallStatus" NOT NULL DEFAULT 'QUEUED',
    "telephonyProvider" TEXT,
    "telephonyCallId" TEXT,
    "phoneFrom" TEXT,
    "phoneTo" TEXT,
    "toNumber" TEXT,
    "fromNumber" TEXT,
    "externalCallId" TEXT,
    "initiatedById" UUID,
    "startedAt" TIMESTAMP(3),
    "answeredAt" TIMESTAMP(3),
    "endedAt" TIMESTAMP(3),
    "duration" INTEGER,
    "durationSeconds" INTEGER,
    "sentimentScore" DOUBLE PRECISION,
    "recordingUrl" TEXT,
    "recordingStorageKey" TEXT,
    "summary" TEXT,
    "sentiment" TEXT,
    "outcome" TEXT,
    "outcomeDetails" JSONB,
    "complianceChecklist" JSONB,
    "knowledgeSourcesUsed" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "latencyStats" JSONB,
    "costBreakdown" JSONB,
    "humanHandoffStatus" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "calls_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "call_transcripts" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "callId" UUID NOT NULL,
    "segments" JSONB NOT NULL DEFAULT '[]',
    "entries" JSONB NOT NULL DEFAULT '[]',
    "fullText" TEXT,
    "wordCount" INTEGER,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "call_transcripts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "call_events" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "callId" UUID NOT NULL,
    "type" TEXT NOT NULL,
    "payload" JSONB NOT NULL DEFAULT '{}',
    "timestamp" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "call_events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "call_recordings" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "callId" UUID NOT NULL,
    "storageKey" TEXT NOT NULL,
    "url" TEXT,
    "duration" INTEGER,
    "format" TEXT,
    "sizeBytes" INTEGER,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "call_recordings_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "knowledge_bases" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "type" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "knowledge_bases_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "knowledge_documents" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "knowledgeBaseId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "sourceUrl" TEXT,
    "storageKey" TEXT,
    "chunkCount" INTEGER NOT NULL DEFAULT 0,
    "chunkStrategy" "ChunkStrategy" NOT NULL DEFAULT 'SENTENCE',
    "chunkSize" INTEGER NOT NULL DEFAULT 512,
    "chunkOverlap" INTEGER NOT NULL DEFAULT 50,
    "status" "DocumentStatus" NOT NULL DEFAULT 'PENDING',
    "processingError" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "knowledge_documents_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "integrations" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "type" TEXT NOT NULL,
    "provider" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "status" TEXT,
    "config" JSONB NOT NULL DEFAULT '{}',
    "credentials" JSONB NOT NULL DEFAULT '{}',
    "fieldMappings" JSONB NOT NULL DEFAULT '{}',
    "webhookUrl" TEXT,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "lastSyncAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "integrations_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "integration_runs" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "integrationId" UUID NOT NULL,
    "status" "IntegrationStatus" NOT NULL DEFAULT 'RUNNING',
    "payload" JSONB,
    "result" JSONB,
    "error" TEXT,
    "startedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "completedAt" TIMESTAMP(3),

    CONSTRAINT "integration_runs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "subscriptions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "plan" TEXT NOT NULL,
    "planId" UUID,
    "status" TEXT NOT NULL,
    "stripeCustomerId" TEXT,
    "stripeSubscriptionId" TEXT,
    "currentPeriodStart" TIMESTAMP(3),
    "currentPeriodEnd" TIMESTAMP(3),
    "seats" INTEGER NOT NULL DEFAULT 1,
    "features" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "subscriptions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "usage_records" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "type" "UsageType" NOT NULL,
    "quantity" DOUBLE PRECISION NOT NULL,
    "unit" TEXT NOT NULL,
    "cost" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "periodStart" TIMESTAMP(3) NOT NULL,
    "periodEnd" TIMESTAMP(3) NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "usage_records_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "audit_logs" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID,
    "userId" UUID,
    "action" TEXT NOT NULL,
    "resource" TEXT,
    "resourceType" TEXT NOT NULL,
    "resourceId" TEXT,
    "entityType" TEXT,
    "details" JSONB,
    "oldValue" JSONB,
    "newValue" JSONB,
    "ipAddress" TEXT,
    "userAgent" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "audit_logs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "notifications" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "userId" UUID,
    "type" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "body" TEXT,
    "channel" "NotificationChannel" NOT NULL DEFAULT 'IN_APP',
    "status" "NotificationStatus" NOT NULL DEFAULT 'PENDING',
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "readAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "notifications_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "feature_flags" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "name" TEXT NOT NULL,
    "description" TEXT,
    "isEnabled" BOOLEAN NOT NULL DEFAULT false,
    "tenantOverrides" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "feature_flags_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "phone_numbers" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "number" TEXT NOT NULL,
    "provider" TEXT NOT NULL,
    "capabilities" JSONB NOT NULL DEFAULT '{}',
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "assignedAgentId" UUID,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "phone_numbers_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "campaign_contacts" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "campaignId" UUID NOT NULL,
    "contactId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "maxAttempts" INTEGER NOT NULL DEFAULT 3,
    "lastCallId" UUID,
    "lastCalledAt" TIMESTAMP(3),
    "nextRetryAt" TIMESTAMP(3),
    "outcome" TEXT,
    "error" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "campaign_contacts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "call_sessions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "callId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "agentId" UUID NOT NULL,
    "workflowId" UUID,
    "currentNodeId" TEXT,
    "workflowState" JSONB NOT NULL DEFAULT '{}',
    "conversationState" JSONB NOT NULL DEFAULT '[]',
    "variables" JSONB NOT NULL DEFAULT '{}',
    "sttProvider" TEXT,
    "ttsProvider" TEXT,
    "llmProvider" TEXT,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "startedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "endedAt" TIMESTAMP(3),

    CONSTRAINT "call_sessions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "document_chunks" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "documentId" UUID NOT NULL,
    "knowledgeBaseId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "content" TEXT NOT NULL,
    "chunkIndex" INTEGER NOT NULL,
    "tokenCount" INTEGER NOT NULL DEFAULT 0,
    "embedding" vector(1536),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "document_chunks_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "crm_syncs" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "integrationId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "direction" "SyncDirection" NOT NULL DEFAULT 'BIDIRECTIONAL',
    "status" "SyncStatus" NOT NULL DEFAULT 'IDLE',
    "fieldMappings" JSONB NOT NULL DEFAULT '[]',
    "lastSyncAt" TIMESTAMP(3),
    "lastSyncCursor" TEXT,
    "syncedRecords" INTEGER NOT NULL DEFAULT 0,
    "failedRecords" INTEGER NOT NULL DEFAULT 0,
    "errorLog" JSONB NOT NULL DEFAULT '[]',
    "config" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "crm_syncs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "crm_field_mappings" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "integrationId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "sourceField" TEXT NOT NULL,
    "targetField" TEXT NOT NULL,
    "direction" "SyncDirection" NOT NULL DEFAULT 'BIDIRECTIONAL',
    "transform" TEXT,
    "isRequired" BOOLEAN NOT NULL DEFAULT false,
    "defaultValue" TEXT,

    CONSTRAINT "crm_field_mappings_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "invoices" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "stripeInvoiceId" TEXT,
    "amount" DOUBLE PRECISION NOT NULL,
    "currency" TEXT NOT NULL DEFAULT 'usd',
    "status" TEXT NOT NULL DEFAULT 'draft',
    "pdfUrl" TEXT,
    "hostedUrl" TEXT,
    "periodStart" TIMESTAMP(3),
    "periodEnd" TIMESTAMP(3),
    "dueDate" TIMESTAMP(3),
    "paidAt" TIMESTAMP(3),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "invoices_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "invoice_line_items" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "invoiceId" UUID NOT NULL,
    "description" TEXT NOT NULL,
    "quantity" DOUBLE PRECISION NOT NULL,
    "unitAmount" DOUBLE PRECISION NOT NULL,
    "amount" DOUBLE PRECISION NOT NULL,
    "usageType" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "invoice_line_items_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "plans" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "name" TEXT NOT NULL,
    "displayName" TEXT NOT NULL,
    "description" TEXT,
    "stripePriceId" TEXT,
    "monthlyPrice" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "annualPrice" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "maxAgents" INTEGER NOT NULL DEFAULT 1,
    "maxCallsPerMonth" INTEGER NOT NULL DEFAULT 100,
    "maxConcurrentCalls" INTEGER NOT NULL DEFAULT 1,
    "maxContacts" INTEGER NOT NULL DEFAULT 500,
    "maxUsers" INTEGER NOT NULL DEFAULT 2,
    "maxKnowledgeBases" INTEGER NOT NULL DEFAULT 1,
    "maxStorageGb" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "includesCallMinutes" INTEGER NOT NULL DEFAULT 60,
    "includesTranscription" BOOLEAN NOT NULL DEFAULT false,
    "includesCrmIntegration" BOOLEAN NOT NULL DEFAULT false,
    "includesAbTesting" BOOLEAN NOT NULL DEFAULT false,
    "includesAdvancedAnalytics" BOOLEAN NOT NULL DEFAULT false,
    "includesCustomWorkflows" BOOLEAN NOT NULL DEFAULT false,
    "features" JSONB NOT NULL DEFAULT '{}',
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "plans_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ab_experiments" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "agentId" UUID NOT NULL,
    "status" "ExperimentStatus" NOT NULL DEFAULT 'DRAFT',
    "trafficSplit" TEXT NOT NULL DEFAULT 'equal',
    "minSampleSize" INTEGER NOT NULL DEFAULT 100,
    "confidenceLevel" DOUBLE PRECISION NOT NULL DEFAULT 0.95,
    "primaryMetric" TEXT NOT NULL DEFAULT 'success_rate',
    "startedAt" TIMESTAMP(3),
    "completedAt" TIMESTAMP(3),
    "winnerId" UUID,
    "createdBy" UUID,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ab_experiments_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ab_variants" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "experimentId" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "systemPrompt" TEXT NOT NULL,
    "trafficPercent" DOUBLE PRECISION NOT NULL DEFAULT 50,
    "isControl" BOOLEAN NOT NULL DEFAULT false,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "totalCalls" INTEGER NOT NULL DEFAULT 0,
    "successfulCalls" INTEGER NOT NULL DEFAULT 0,
    "failedCalls" INTEGER NOT NULL DEFAULT 0,
    "totalDuration" INTEGER NOT NULL DEFAULT 0,
    "avgSentiment" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "conversions" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "ab_variants_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "analytics_reports" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "type" "ReportType" NOT NULL,
    "name" TEXT NOT NULL,
    "status" "ReportStatus" NOT NULL DEFAULT 'PENDING',
    "parameters" JSONB NOT NULL DEFAULT '{}',
    "storageKey" TEXT,
    "fileUrl" TEXT,
    "fileSize" INTEGER,
    "format" TEXT NOT NULL DEFAULT 'pdf',
    "createdBy" UUID,
    "startedAt" TIMESTAMP(3),
    "completedAt" TIMESTAMP(3),
    "expiresAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "analytics_reports_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "analytics_snapshots" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenantId" UUID NOT NULL,
    "period" TEXT NOT NULL,
    "periodKey" TEXT NOT NULL,
    "metrics" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "analytics_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "workflow_executions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "workflowId" UUID NOT NULL,
    "callId" UUID,
    "tenantId" UUID NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'running',
    "currentNodeId" TEXT,
    "variables" JSONB NOT NULL DEFAULT '{}',
    "executionLog" JSONB NOT NULL DEFAULT '[]',
    "startedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "completedAt" TIMESTAMP(3),
    "error" TEXT,

    CONSTRAINT "workflow_executions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "kb_documents" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "knowledgeBaseId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "fileName" TEXT NOT NULL,
    "contentType" TEXT,
    "fileKey" TEXT,
    "status" TEXT NOT NULL DEFAULT 'PENDING',
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "kb_documents_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "kb_chunks" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "knowledgeBaseId" UUID NOT NULL,
    "documentId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "content" TEXT NOT NULL,
    "chunkIndex" INTEGER NOT NULL,
    "embedding" vector(1536),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "kb_chunks_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "notification_preferences" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "userId" UUID NOT NULL,
    "tenantId" UUID NOT NULL,
    "emailEnabled" BOOLEAN NOT NULL DEFAULT true,
    "inAppEnabled" BOOLEAN NOT NULL DEFAULT true,
    "webhookEnabled" BOOLEAN NOT NULL DEFAULT false,
    "emailDigest" TEXT NOT NULL DEFAULT 'DAILY',
    "mutedTypes" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "notification_preferences_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "tenants_slug_key" ON "tenants"("slug");

-- CreateIndex
CREATE INDEX "tenants_slug_idx" ON "tenants"("slug");

-- CreateIndex
CREATE INDEX "tenants_status_idx" ON "tenants"("status");

-- CreateIndex
CREATE INDEX "tenants_plan_idx" ON "tenants"("plan");

-- CreateIndex
CREATE INDEX "tenants_createdAt_idx" ON "tenants"("createdAt");

-- CreateIndex
CREATE INDEX "users_tenantId_idx" ON "users"("tenantId");

-- CreateIndex
CREATE INDEX "users_email_idx" ON "users"("email");

-- CreateIndex
CREATE INDEX "users_tenantId_isActive_idx" ON "users"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "users_tenantId_role_idx" ON "users"("tenantId", "role");

-- CreateIndex
CREATE UNIQUE INDEX "users_email_tenantId_key" ON "users"("email", "tenantId");

-- CreateIndex
CREATE INDEX "memberships_tenantId_idx" ON "memberships"("tenantId");

-- CreateIndex
CREATE INDEX "memberships_userId_idx" ON "memberships"("userId");

-- CreateIndex
CREATE INDEX "agents_tenantId_idx" ON "agents"("tenantId");

-- CreateIndex
CREATE INDEX "agents_tenantId_isActive_idx" ON "agents"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "agents_tenantId_type_idx" ON "agents"("tenantId", "type");

-- CreateIndex
CREATE INDEX "agents_tenantId_industry_idx" ON "agents"("tenantId", "industry");

-- CreateIndex
CREATE INDEX "agents_knowledgeBaseId_idx" ON "agents"("knowledgeBaseId");

-- CreateIndex
CREATE INDEX "agent_versions_agentId_idx" ON "agent_versions"("agentId");

-- CreateIndex
CREATE INDEX "agent_versions_tenantId_idx" ON "agent_versions"("tenantId");

-- CreateIndex
CREATE UNIQUE INDEX "agent_versions_agentId_version_key" ON "agent_versions"("agentId", "version");

-- CreateIndex
CREATE INDEX "workflows_tenantId_idx" ON "workflows"("tenantId");

-- CreateIndex
CREATE INDEX "workflows_tenantId_isActive_idx" ON "workflows"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "workflows_agentId_idx" ON "workflows"("agentId");

-- CreateIndex
CREATE INDEX "workflow_versions_workflowId_idx" ON "workflow_versions"("workflowId");

-- CreateIndex
CREATE INDEX "workflow_versions_tenantId_idx" ON "workflow_versions"("tenantId");

-- CreateIndex
CREATE UNIQUE INDEX "workflow_versions_workflowId_version_key" ON "workflow_versions"("workflowId", "version");

-- CreateIndex
CREATE INDEX "contacts_tenantId_idx" ON "contacts"("tenantId");

-- CreateIndex
CREATE INDEX "contacts_tenantId_email_idx" ON "contacts"("tenantId", "email");

-- CreateIndex
CREATE INDEX "contacts_tenantId_phone_idx" ON "contacts"("tenantId", "phone");

-- CreateIndex
CREATE INDEX "contacts_tenantId_segment_idx" ON "contacts"("tenantId", "segment");

-- CreateIndex
CREATE INDEX "contacts_tenantId_consentStatus_idx" ON "contacts"("tenantId", "consentStatus");

-- CreateIndex
CREATE INDEX "contacts_tenantId_dncStatus_idx" ON "contacts"("tenantId", "dncStatus");

-- CreateIndex
CREATE INDEX "contacts_tenantId_source_idx" ON "contacts"("tenantId", "source");

-- CreateIndex
CREATE INDEX "contacts_tenantId_externalId_idx" ON "contacts"("tenantId", "externalId");

-- CreateIndex
CREATE INDEX "contacts_tags_idx" ON "contacts" USING GIN ("tags");

-- CreateIndex
CREATE INDEX "contacts_accountId_idx" ON "contacts"("accountId");

-- CreateIndex
CREATE INDEX "accounts_tenantId_idx" ON "accounts"("tenantId");

-- CreateIndex
CREATE INDEX "accounts_tenantId_name_idx" ON "accounts"("tenantId", "name");

-- CreateIndex
CREATE INDEX "contact_notes_contactId_idx" ON "contact_notes"("contactId");

-- CreateIndex
CREATE INDEX "campaigns_tenantId_idx" ON "campaigns"("tenantId");

-- CreateIndex
CREATE INDEX "campaigns_tenantId_status_idx" ON "campaigns"("tenantId", "status");

-- CreateIndex
CREATE INDEX "campaigns_tenantId_agentId_idx" ON "campaigns"("tenantId", "agentId");

-- CreateIndex
CREATE INDEX "campaigns_status_idx" ON "campaigns"("status");

-- CreateIndex
CREATE INDEX "campaigns_scheduledAt_idx" ON "campaigns"("scheduledAt");

-- CreateIndex
CREATE INDEX "campaigns_type_idx" ON "campaigns"("type");

-- CreateIndex
CREATE INDEX "calls_tenantId_idx" ON "calls"("tenantId");

-- CreateIndex
CREATE INDEX "calls_tenantId_status_idx" ON "calls"("tenantId", "status");

-- CreateIndex
CREATE INDEX "calls_tenantId_agentId_idx" ON "calls"("tenantId", "agentId");

-- CreateIndex
CREATE INDEX "calls_tenantId_campaignId_idx" ON "calls"("tenantId", "campaignId");

-- CreateIndex
CREATE INDEX "calls_tenantId_contactId_idx" ON "calls"("tenantId", "contactId");

-- CreateIndex
CREATE INDEX "calls_tenantId_direction_idx" ON "calls"("tenantId", "direction");

-- CreateIndex
CREATE INDEX "calls_tenantId_createdAt_idx" ON "calls"("tenantId", "createdAt");

-- CreateIndex
CREATE INDEX "calls_tenantId_outcome_idx" ON "calls"("tenantId", "outcome");

-- CreateIndex
CREATE INDEX "calls_telephonyCallId_idx" ON "calls"("telephonyCallId");

-- CreateIndex
CREATE INDEX "calls_campaignId_status_idx" ON "calls"("campaignId", "status");

-- CreateIndex
CREATE INDEX "calls_startedAt_idx" ON "calls"("startedAt");

-- CreateIndex
CREATE UNIQUE INDEX "call_transcripts_callId_key" ON "call_transcripts"("callId");

-- CreateIndex
CREATE INDEX "call_transcripts_callId_idx" ON "call_transcripts"("callId");

-- CreateIndex
CREATE INDEX "call_events_callId_idx" ON "call_events"("callId");

-- CreateIndex
CREATE INDEX "call_events_callId_type_idx" ON "call_events"("callId", "type");

-- CreateIndex
CREATE INDEX "call_events_callId_timestamp_idx" ON "call_events"("callId", "timestamp");

-- CreateIndex
CREATE INDEX "call_recordings_callId_idx" ON "call_recordings"("callId");

-- CreateIndex
CREATE INDEX "knowledge_bases_tenantId_idx" ON "knowledge_bases"("tenantId");

-- CreateIndex
CREATE INDEX "knowledge_documents_knowledgeBaseId_idx" ON "knowledge_documents"("knowledgeBaseId");

-- CreateIndex
CREATE INDEX "knowledge_documents_knowledgeBaseId_status_idx" ON "knowledge_documents"("knowledgeBaseId", "status");

-- CreateIndex
CREATE INDEX "integrations_tenantId_idx" ON "integrations"("tenantId");

-- CreateIndex
CREATE INDEX "integrations_tenantId_type_idx" ON "integrations"("tenantId", "type");

-- CreateIndex
CREATE INDEX "integrations_tenantId_provider_idx" ON "integrations"("tenantId", "provider");

-- CreateIndex
CREATE INDEX "integrations_tenantId_isActive_idx" ON "integrations"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "integration_runs_integrationId_idx" ON "integration_runs"("integrationId");

-- CreateIndex
CREATE INDEX "integration_runs_integrationId_status_idx" ON "integration_runs"("integrationId", "status");

-- CreateIndex
CREATE UNIQUE INDEX "subscriptions_tenantId_key" ON "subscriptions"("tenantId");

-- CreateIndex
CREATE INDEX "subscriptions_stripeCustomerId_idx" ON "subscriptions"("stripeCustomerId");

-- CreateIndex
CREATE INDEX "subscriptions_stripeSubscriptionId_idx" ON "subscriptions"("stripeSubscriptionId");

-- CreateIndex
CREATE INDEX "subscriptions_status_idx" ON "subscriptions"("status");

-- CreateIndex
CREATE INDEX "subscriptions_planId_idx" ON "subscriptions"("planId");

-- CreateIndex
CREATE INDEX "usage_records_tenantId_idx" ON "usage_records"("tenantId");

-- CreateIndex
CREATE INDEX "usage_records_tenantId_type_idx" ON "usage_records"("tenantId", "type");

-- CreateIndex
CREATE INDEX "usage_records_tenantId_periodStart_periodEnd_idx" ON "usage_records"("tenantId", "periodStart", "periodEnd");

-- CreateIndex
CREATE INDEX "usage_records_tenantId_type_periodStart_idx" ON "usage_records"("tenantId", "type", "periodStart");

-- CreateIndex
CREATE INDEX "audit_logs_tenantId_idx" ON "audit_logs"("tenantId");

-- CreateIndex
CREATE INDEX "audit_logs_tenantId_action_idx" ON "audit_logs"("tenantId", "action");

-- CreateIndex
CREATE INDEX "audit_logs_tenantId_resourceType_idx" ON "audit_logs"("tenantId", "resourceType");

-- CreateIndex
CREATE INDEX "audit_logs_tenantId_resourceType_resourceId_idx" ON "audit_logs"("tenantId", "resourceType", "resourceId");

-- CreateIndex
CREATE INDEX "audit_logs_userId_idx" ON "audit_logs"("userId");

-- CreateIndex
CREATE INDEX "audit_logs_createdAt_idx" ON "audit_logs"("createdAt");

-- CreateIndex
CREATE INDEX "notifications_tenantId_idx" ON "notifications"("tenantId");

-- CreateIndex
CREATE INDEX "notifications_userId_idx" ON "notifications"("userId");

-- CreateIndex
CREATE INDEX "notifications_tenantId_userId_status_idx" ON "notifications"("tenantId", "userId", "status");

-- CreateIndex
CREATE INDEX "notifications_tenantId_channel_idx" ON "notifications"("tenantId", "channel");

-- CreateIndex
CREATE INDEX "notifications_createdAt_idx" ON "notifications"("createdAt");

-- CreateIndex
CREATE UNIQUE INDEX "feature_flags_name_key" ON "feature_flags"("name");

-- CreateIndex
CREATE INDEX "feature_flags_name_idx" ON "feature_flags"("name");

-- CreateIndex
CREATE INDEX "feature_flags_isEnabled_idx" ON "feature_flags"("isEnabled");

-- CreateIndex
CREATE UNIQUE INDEX "phone_numbers_number_key" ON "phone_numbers"("number");

-- CreateIndex
CREATE INDEX "phone_numbers_tenantId_idx" ON "phone_numbers"("tenantId");

-- CreateIndex
CREATE INDEX "phone_numbers_tenantId_isActive_idx" ON "phone_numbers"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "phone_numbers_number_idx" ON "phone_numbers"("number");

-- CreateIndex
CREATE INDEX "phone_numbers_assignedAgentId_idx" ON "phone_numbers"("assignedAgentId");

-- CreateIndex
CREATE INDEX "campaign_contacts_campaignId_status_idx" ON "campaign_contacts"("campaignId", "status");

-- CreateIndex
CREATE INDEX "campaign_contacts_campaignId_nextRetryAt_idx" ON "campaign_contacts"("campaignId", "nextRetryAt");

-- CreateIndex
CREATE INDEX "campaign_contacts_tenantId_idx" ON "campaign_contacts"("tenantId");

-- CreateIndex
CREATE UNIQUE INDEX "campaign_contacts_campaignId_contactId_key" ON "campaign_contacts"("campaignId", "contactId");

-- CreateIndex
CREATE UNIQUE INDEX "call_sessions_callId_key" ON "call_sessions"("callId");

-- CreateIndex
CREATE INDEX "call_sessions_callId_idx" ON "call_sessions"("callId");

-- CreateIndex
CREATE INDEX "call_sessions_tenantId_isActive_idx" ON "call_sessions"("tenantId", "isActive");

-- CreateIndex
CREATE INDEX "document_chunks_documentId_idx" ON "document_chunks"("documentId");

-- CreateIndex
CREATE INDEX "document_chunks_knowledgeBaseId_idx" ON "document_chunks"("knowledgeBaseId");

-- CreateIndex
CREATE INDEX "document_chunks_tenantId_knowledgeBaseId_idx" ON "document_chunks"("tenantId", "knowledgeBaseId");

-- CreateIndex
CREATE INDEX "crm_syncs_integrationId_idx" ON "crm_syncs"("integrationId");

-- CreateIndex
CREATE INDEX "crm_syncs_tenantId_idx" ON "crm_syncs"("tenantId");

-- CreateIndex
CREATE INDEX "crm_syncs_tenantId_status_idx" ON "crm_syncs"("tenantId", "status");

-- CreateIndex
CREATE INDEX "crm_field_mappings_integrationId_idx" ON "crm_field_mappings"("integrationId");

-- CreateIndex
CREATE INDEX "crm_field_mappings_tenantId_idx" ON "crm_field_mappings"("tenantId");

-- CreateIndex
CREATE UNIQUE INDEX "crm_field_mappings_integrationId_sourceField_targetField_key" ON "crm_field_mappings"("integrationId", "sourceField", "targetField");

-- CreateIndex
CREATE UNIQUE INDEX "invoices_stripeInvoiceId_key" ON "invoices"("stripeInvoiceId");

-- CreateIndex
CREATE INDEX "invoices_tenantId_idx" ON "invoices"("tenantId");

-- CreateIndex
CREATE INDEX "invoices_tenantId_status_idx" ON "invoices"("tenantId", "status");

-- CreateIndex
CREATE INDEX "invoices_stripeInvoiceId_idx" ON "invoices"("stripeInvoiceId");

-- CreateIndex
CREATE INDEX "invoice_line_items_invoiceId_idx" ON "invoice_line_items"("invoiceId");

-- CreateIndex
CREATE UNIQUE INDEX "plans_name_key" ON "plans"("name");

-- CreateIndex
CREATE UNIQUE INDEX "plans_stripePriceId_key" ON "plans"("stripePriceId");

-- CreateIndex
CREATE INDEX "plans_isActive_idx" ON "plans"("isActive");

-- CreateIndex
CREATE INDEX "plans_name_idx" ON "plans"("name");

-- CreateIndex
CREATE INDEX "ab_experiments_tenantId_idx" ON "ab_experiments"("tenantId");

-- CreateIndex
CREATE INDEX "ab_experiments_tenantId_status_idx" ON "ab_experiments"("tenantId", "status");

-- CreateIndex
CREATE INDEX "ab_experiments_tenantId_agentId_idx" ON "ab_experiments"("tenantId", "agentId");

-- CreateIndex
CREATE INDEX "ab_variants_experimentId_idx" ON "ab_variants"("experimentId");

-- CreateIndex
CREATE INDEX "analytics_reports_tenantId_idx" ON "analytics_reports"("tenantId");

-- CreateIndex
CREATE INDEX "analytics_reports_tenantId_type_idx" ON "analytics_reports"("tenantId", "type");

-- CreateIndex
CREATE INDEX "analytics_reports_tenantId_status_idx" ON "analytics_reports"("tenantId", "status");

-- CreateIndex
CREATE INDEX "analytics_reports_createdAt_idx" ON "analytics_reports"("createdAt");

-- CreateIndex
CREATE INDEX "analytics_snapshots_tenantId_period_idx" ON "analytics_snapshots"("tenantId", "period");

-- CreateIndex
CREATE INDEX "analytics_snapshots_tenantId_periodKey_idx" ON "analytics_snapshots"("tenantId", "periodKey");

-- CreateIndex
CREATE UNIQUE INDEX "analytics_snapshots_tenantId_period_periodKey_key" ON "analytics_snapshots"("tenantId", "period", "periodKey");

-- CreateIndex
CREATE INDEX "workflow_executions_workflowId_idx" ON "workflow_executions"("workflowId");

-- CreateIndex
CREATE INDEX "workflow_executions_callId_idx" ON "workflow_executions"("callId");

-- CreateIndex
CREATE INDEX "workflow_executions_tenantId_idx" ON "workflow_executions"("tenantId");

-- CreateIndex
CREATE INDEX "workflow_executions_tenantId_status_idx" ON "workflow_executions"("tenantId", "status");

-- CreateIndex
CREATE INDEX "kb_documents_knowledgeBaseId_idx" ON "kb_documents"("knowledgeBaseId");

-- CreateIndex
CREATE INDEX "kb_documents_tenantId_idx" ON "kb_documents"("tenantId");

-- CreateIndex
CREATE INDEX "kb_documents_knowledgeBaseId_status_idx" ON "kb_documents"("knowledgeBaseId", "status");

-- CreateIndex
CREATE INDEX "kb_chunks_knowledgeBaseId_idx" ON "kb_chunks"("knowledgeBaseId");

-- CreateIndex
CREATE INDEX "kb_chunks_documentId_idx" ON "kb_chunks"("documentId");

-- CreateIndex
CREATE INDEX "kb_chunks_tenantId_idx" ON "kb_chunks"("tenantId");

-- CreateIndex
CREATE INDEX "kb_chunks_tenantId_knowledgeBaseId_idx" ON "kb_chunks"("tenantId", "knowledgeBaseId");

-- CreateIndex
CREATE INDEX "notification_preferences_userId_idx" ON "notification_preferences"("userId");

-- CreateIndex
CREATE INDEX "notification_preferences_tenantId_idx" ON "notification_preferences"("tenantId");

-- CreateIndex
CREATE UNIQUE INDEX "notification_preferences_userId_tenantId_key" ON "notification_preferences"("userId", "tenantId");

-- AddForeignKey
ALTER TABLE "users" ADD CONSTRAINT "users_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "memberships" ADD CONSTRAINT "memberships_userId_fkey" FOREIGN KEY ("userId") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "memberships" ADD CONSTRAINT "memberships_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "agents" ADD CONSTRAINT "agents_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "agents" ADD CONSTRAINT "agents_knowledgeBaseId_fkey" FOREIGN KEY ("knowledgeBaseId") REFERENCES "knowledge_bases"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "agent_versions" ADD CONSTRAINT "agent_versions_agentId_fkey" FOREIGN KEY ("agentId") REFERENCES "agents"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "workflows" ADD CONSTRAINT "workflows_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "workflow_versions" ADD CONSTRAINT "workflow_versions_workflowId_fkey" FOREIGN KEY ("workflowId") REFERENCES "workflows"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "contacts" ADD CONSTRAINT "contacts_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "contacts" ADD CONSTRAINT "contacts_accountId_fkey" FOREIGN KEY ("accountId") REFERENCES "accounts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "accounts" ADD CONSTRAINT "accounts_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "contact_notes" ADD CONSTRAINT "contact_notes_contactId_fkey" FOREIGN KEY ("contactId") REFERENCES "contacts"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "campaigns" ADD CONSTRAINT "campaigns_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "campaigns" ADD CONSTRAINT "campaigns_agentId_fkey" FOREIGN KEY ("agentId") REFERENCES "agents"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "campaigns" ADD CONSTRAINT "campaigns_workflowId_fkey" FOREIGN KEY ("workflowId") REFERENCES "workflows"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "calls" ADD CONSTRAINT "calls_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "calls" ADD CONSTRAINT "calls_campaignId_fkey" FOREIGN KEY ("campaignId") REFERENCES "campaigns"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "calls" ADD CONSTRAINT "calls_agentId_fkey" FOREIGN KEY ("agentId") REFERENCES "agents"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "calls" ADD CONSTRAINT "calls_contactId_fkey" FOREIGN KEY ("contactId") REFERENCES "contacts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "call_transcripts" ADD CONSTRAINT "call_transcripts_callId_fkey" FOREIGN KEY ("callId") REFERENCES "calls"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "call_events" ADD CONSTRAINT "call_events_callId_fkey" FOREIGN KEY ("callId") REFERENCES "calls"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "call_recordings" ADD CONSTRAINT "call_recordings_callId_fkey" FOREIGN KEY ("callId") REFERENCES "calls"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_bases" ADD CONSTRAINT "knowledge_bases_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "knowledge_documents" ADD CONSTRAINT "knowledge_documents_knowledgeBaseId_fkey" FOREIGN KEY ("knowledgeBaseId") REFERENCES "knowledge_bases"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "integrations" ADD CONSTRAINT "integrations_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "integration_runs" ADD CONSTRAINT "integration_runs_integrationId_fkey" FOREIGN KEY ("integrationId") REFERENCES "integrations"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "subscriptions" ADD CONSTRAINT "subscriptions_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "subscriptions" ADD CONSTRAINT "subscriptions_planId_fkey" FOREIGN KEY ("planId") REFERENCES "plans"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "usage_records" ADD CONSTRAINT "usage_records_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "audit_logs" ADD CONSTRAINT "audit_logs_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "audit_logs" ADD CONSTRAINT "audit_logs_userId_fkey" FOREIGN KEY ("userId") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "notifications" ADD CONSTRAINT "notifications_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "notifications" ADD CONSTRAINT "notifications_userId_fkey" FOREIGN KEY ("userId") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "phone_numbers" ADD CONSTRAINT "phone_numbers_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "phone_numbers" ADD CONSTRAINT "phone_numbers_assignedAgentId_fkey" FOREIGN KEY ("assignedAgentId") REFERENCES "agents"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "document_chunks" ADD CONSTRAINT "document_chunks_documentId_fkey" FOREIGN KEY ("documentId") REFERENCES "knowledge_documents"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "invoice_line_items" ADD CONSTRAINT "invoice_line_items_invoiceId_fkey" FOREIGN KEY ("invoiceId") REFERENCES "invoices"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ab_variants" ADD CONSTRAINT "ab_variants_experimentId_fkey" FOREIGN KEY ("experimentId") REFERENCES "ab_experiments"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "kb_documents" ADD CONSTRAINT "kb_documents_knowledgeBaseId_fkey" FOREIGN KEY ("knowledgeBaseId") REFERENCES "knowledge_bases"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "kb_chunks" ADD CONSTRAINT "kb_chunks_knowledgeBaseId_fkey" FOREIGN KEY ("knowledgeBaseId") REFERENCES "knowledge_bases"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "kb_chunks" ADD CONSTRAINT "kb_chunks_documentId_fkey" FOREIGN KEY ("documentId") REFERENCES "kb_documents"("id") ON DELETE CASCADE ON UPDATE CASCADE;

