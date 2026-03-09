import { PrismaClient } from '@prisma/client';
import * as bcrypt from 'bcryptjs';

const prisma = new PrismaClient();

// ─── Helpers ────────────────────────────────────────────────────────────────

function daysAgo(n: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

function hoursAgo(n: number): Date {
  const d = new Date();
  d.setHours(d.getHours() - n);
  return d;
}

function randomPhone(): string {
  const area = Math.floor(200 + Math.random() * 800);
  const mid = Math.floor(100 + Math.random() * 900);
  const last = Math.floor(1000 + Math.random() * 9000);
  return `+1${area}${mid}${last}`;
}

function randomDuration(): number {
  return Math.floor(30 + Math.random() * 570); // 30-600 seconds
}

function pickRandom<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)]!;
}

const CALL_STATUSES = [
  'COMPLETED',
  'COMPLETED',
  'COMPLETED',
  'NO_ANSWER',
  'BUSY',
  'VOICEMAIL',
  'FAILED',
] as const;

const CALL_DIRECTIONS = ['OUTBOUND', 'INBOUND'] as const;

const SENTIMENTS = ['positive', 'neutral', 'negative'] as const;

// ─── Password hashing ──────────────────────────────────────────────────────

async function hashPassword(raw: string): Promise<string> {
  return bcrypt.hash(raw, 10);
}

// ─── Feature flags ─────────────────────────────────────────────────────────

async function seedFeatureFlags() {
  const flags = [
    {
      key: 'advanced_analytics',
      name: 'Advanced Analytics Dashboard',
      description: 'Enables advanced analytics with sentiment trends, cohort analysis, and predictive metrics.',
      enabled: true,
      allowedPlans: ['professional', 'enterprise'],
    },
    {
      key: 'knowledge_base_rag',
      name: 'Knowledge Base RAG',
      description: 'Retrieval-Augmented Generation for knowledge base documents during live calls.',
      enabled: true,
      allowedPlans: ['professional', 'enterprise'],
    },
    {
      key: 'custom_integrations',
      name: 'Custom Integrations',
      description: 'Allows creating custom webhook and API integrations with third-party systems.',
      enabled: true,
      allowedPlans: ['enterprise'],
    },
    {
      key: 'voice_cloning',
      name: 'Voice Cloning',
      description: 'AI voice cloning for branded agent voices.',
      enabled: false,
      allowedPlans: ['enterprise'],
    },
    {
      key: 'multilingual_switching',
      name: 'Multilingual Live Switching',
      description: 'Real-time language detection and switching during live calls.',
      enabled: false,
      allowedPlans: ['enterprise'],
    },
  ];

  for (const flag of flags) {
    await prisma.featureFlag.upsert({
      where: { key: flag.key },
      update: {},
      create: {
        key: flag.key,
        name: flag.name,
        description: flag.description,
        enabled: flag.enabled,
        metadata: { allowedPlans: flag.allowedPlans },
      },
    });
  }

  console.log('  ✓ Feature flags seeded');
}

// ─── Super Admin ───────────────────────────────────────────────────────────

async function seedSuperAdmin() {
  const password = await hashPassword('SuperAdmin123!');

  await prisma.user.upsert({
    where: { email: 'admin@voiceai.platform' },
    update: {},
    create: {
      email: 'admin@voiceai.platform',
      passwordHash: password,
      firstName: 'Platform',
      lastName: 'Admin',
      role: 'SUPER_ADMIN',
      isActive: true,
      emailVerified: true,
      lastLoginAt: new Date(),
    },
  });

  console.log('  ✓ Super admin seeded (admin@voiceai.platform / SuperAdmin123!)');
}

// ─── Transcript builders ───────────────────────────────────────────────────

function buildHealthcareAppointmentTranscript() {
  return [
    { role: 'agent', text: "Hello, this is Sarah calling from MedCare Health Systems. Am I speaking with the patient on file?", timestamp: 0, sentiment: 'neutral' },
    { role: 'contact', text: "Yes, hi. This is speaking.", timestamp: 3.5, sentiment: 'neutral' },
    { role: 'agent', text: "Great! I'm calling to remind you about your upcoming wellness check. We have availability this Thursday at 10 AM or Friday at 2 PM. Would either of those work for you?", timestamp: 6.2, sentiment: 'positive' },
    { role: 'contact', text: "Thursday at 10 works perfectly for me.", timestamp: 14.8, sentiment: 'positive' },
    { role: 'agent', text: "Wonderful! I've booked you for Thursday at 10 AM with Dr. Chen. Please remember to fast for 12 hours before your appointment for the blood work. Is there anything else I can help with?", timestamp: 18.1, sentiment: 'positive' },
    { role: 'contact', text: "No, that's all. Thank you for the reminder!", timestamp: 27.3, sentiment: 'positive' },
    { role: 'agent', text: "You're welcome! We'll send a confirmation text shortly. Have a great day!", timestamp: 30.5, sentiment: 'positive' },
  ];
}

function buildHealthcareFollowUpTranscript() {
  return [
    { role: 'agent', text: "Good afternoon, this is MedCare Health Systems calling for a post-visit follow-up. How are you feeling since your appointment last week?", timestamp: 0, sentiment: 'neutral' },
    { role: 'contact', text: "Hi, I'm actually feeling much better. The medication Dr. Patel prescribed is helping.", timestamp: 4.2, sentiment: 'positive' },
    { role: 'agent', text: "That's wonderful to hear! Have you experienced any side effects?", timestamp: 10.1, sentiment: 'positive' },
    { role: 'contact', text: "A little drowsiness in the morning, but nothing too bad.", timestamp: 14.6, sentiment: 'neutral' },
    { role: 'agent', text: "That's a common and usually temporary side effect. If it persists beyond two weeks, please contact us. Your follow-up lab work is scheduled for March 20th. Will that still work?", timestamp: 18.0, sentiment: 'neutral' },
    { role: 'contact', text: "Yes, that works. Thank you for checking in!", timestamp: 27.5, sentiment: 'positive' },
  ];
}

function buildCollectionsPaymentTranscript() {
  return [
    { role: 'agent', text: "Good morning, this is FinanceFirst Collections calling regarding your account ending in 4829. We show an overdue balance of $2,340.00. Is this a good time to discuss payment options?", timestamp: 0, sentiment: 'neutral' },
    { role: 'contact', text: "Oh, yes. I know I'm behind. I had some unexpected medical expenses last month.", timestamp: 6.8, sentiment: 'negative' },
    { role: 'agent', text: "I understand, and I appreciate you speaking with us. We have several options available. We can set up a payment plan of $390 per month over 6 months, or if you can make a lump sum payment of $1,900, we can settle the account today.", timestamp: 12.4, sentiment: 'neutral' },
    { role: 'contact', text: "The payment plan sounds more manageable. Can I start next Friday when I get paid?", timestamp: 24.0, sentiment: 'neutral' },
    { role: 'agent', text: "Absolutely. I'll set up your first payment of $390 for next Friday, March 14th. You'll receive a confirmation email with all the details. Is there anything else you'd like to know?", timestamp: 29.3, sentiment: 'positive' },
    { role: 'contact', text: "No, that's clear. Thank you for being understanding.", timestamp: 37.8, sentiment: 'positive' },
    { role: 'agent', text: "Of course. We're here to help you get back on track. Have a good day!", timestamp: 41.2, sentiment: 'positive' },
  ];
}

function buildCollectionsDisputeTranscript() {
  return [
    { role: 'agent', text: "Hello, this is FinanceFirst Collections regarding account ending in 7156. We have a balance of $1,875.50 past due. May I discuss this with you?", timestamp: 0, sentiment: 'neutral' },
    { role: 'contact', text: "I actually want to dispute this charge. I returned the product and should have received a refund.", timestamp: 4.5, sentiment: 'negative' },
    { role: 'agent', text: "I understand your concern. Let me note this as a dispute on your account. Do you have a return confirmation number or tracking information?", timestamp: 10.2, sentiment: 'neutral' },
    { role: 'contact', text: "Yes, the return tracking number is RT-2024-88431. I shipped it back on February 15th.", timestamp: 16.0, sentiment: 'neutral' },
    { role: 'agent', text: "Thank you. I've recorded the dispute with tracking number RT-2024-88431. Our team will investigate this within 5 business days and contact you with the resolution. In the meantime, your account is placed on hold.", timestamp: 21.5, sentiment: 'neutral' },
    { role: 'contact', text: "Good, thank you. I'd appreciate a quick resolution.", timestamp: 31.0, sentiment: 'neutral' },
  ];
}

function buildLegalIntakeTranscript() {
  return [
    { role: 'agent', text: "Thank you for calling LegalEagle Partners. My name is Alex, and I'm here to help with your initial consultation request. Could you briefly describe your legal matter?", timestamp: 0, sentiment: 'neutral' },
    { role: 'contact', text: "Yes, I was in a car accident about three weeks ago. The other driver ran a red light and I've had significant medical bills piling up.", timestamp: 5.6, sentiment: 'negative' },
    { role: 'agent', text: "I'm sorry to hear about your accident. That sounds like a personal injury case. Were there any witnesses or a police report filed at the scene?", timestamp: 14.2, sentiment: 'neutral' },
    { role: 'contact', text: "Yes, the police came and filed a report. The other driver was cited for running the red light. I also have dashcam footage.", timestamp: 20.8, sentiment: 'neutral' },
    { role: 'agent', text: "That's very helpful for your case. I'd like to schedule you for a free 30-minute consultation with one of our personal injury attorneys. We have availability tomorrow at 3 PM or Wednesday at 11 AM. Which works better?", timestamp: 28.0, sentiment: 'positive' },
    { role: 'contact', text: "Wednesday at 11 AM would be great.", timestamp: 37.5, sentiment: 'positive' },
    { role: 'agent', text: "Perfect. I've scheduled you for Wednesday at 11 AM with Attorney Martinez. Please bring your police report, medical records, and dashcam footage. We'll send a confirmation email with our office address and a pre-consultation questionnaire.", timestamp: 40.0, sentiment: 'positive' },
  ];
}

function buildSalesQualificationTranscript() {
  return [
    { role: 'agent', text: "Hi, this is Jordan from SalesForce Elite. I noticed you downloaded our enterprise automation whitepaper last week. I'd love to learn more about your team's needs. Do you have a few minutes?", timestamp: 0, sentiment: 'positive' },
    { role: 'contact', text: "Sure, we're actually evaluating a few solutions right now. We have a team of about 50 SDRs and our current dialer is pretty outdated.", timestamp: 5.8, sentiment: 'neutral' },
    { role: 'agent', text: "That's great timing then. With 50 SDRs, you'd likely benefit from our Enterprise plan which includes AI-powered lead scoring and automated follow-ups. What's your biggest pain point with the current system?", timestamp: 13.5, sentiment: 'positive' },
    { role: 'contact', text: "Honestly, it's the manual data entry. Our reps spend about 30% of their day logging calls and updating the CRM instead of actually selling.", timestamp: 21.0, sentiment: 'negative' },
    { role: 'agent', text: "That's exactly what our platform solves. Our AI automatically transcribes calls, extracts action items, and syncs everything to your CRM in real-time. Customers typically see a 40% increase in talk time after implementation. Would you be interested in a personalized demo?", timestamp: 28.3, sentiment: 'positive' },
    { role: 'contact', text: "Yes, definitely. Can we set something up for next week? I'd want to include our VP of Sales as well.", timestamp: 38.0, sentiment: 'positive' },
    { role: 'agent', text: "Absolutely! I'll send a calendar invite for next Tuesday at 2 PM. I'll include a brief agenda and some case studies from similar-sized teams. Looking forward to it!", timestamp: 42.5, sentiment: 'positive' },
  ];
}

function buildSalesDemoBookingTranscript() {
  return [
    { role: 'agent', text: "Good morning! This is Casey from SalesForce Elite following up on your demo request. Just confirming — you'd like to see our voice AI platform in action?", timestamp: 0, sentiment: 'positive' },
    { role: 'contact', text: "Yes, that's right. We're particularly interested in the campaign automation features.", timestamp: 4.2, sentiment: 'neutral' },
    { role: 'agent', text: "Perfect. Our campaign module lets you set up multi-touch outreach sequences with AI-driven call scheduling. Let me walk you through a quick overview. How many contacts do you typically run campaigns to?", timestamp: 8.0, sentiment: 'positive' },
    { role: 'contact', text: "We usually have campaigns of 500 to 2,000 contacts at a time.", timestamp: 16.5, sentiment: 'neutral' },
    { role: 'agent', text: "That's right in our sweet spot. I've scheduled your live demo for Thursday at 10 AM Pacific. You'll get a personalized sandbox environment to test with. I'll also include our ROI calculator so you can see projected savings.", timestamp: 20.0, sentiment: 'positive' },
    { role: 'contact', text: "Sounds great. Can you send the details to my email?", timestamp: 30.2, sentiment: 'positive' },
    { role: 'agent', text: "Absolutely, sending that right over now. See you Thursday!", timestamp: 33.0, sentiment: 'positive' },
  ];
}

// ─── Main seed for a single tenant ─────────────────────────────────────────

interface TenantSeedConfig {
  name: string;
  slug: string;
  industry: string;
  plan: string;
  users: { email: string; firstName: string; lastName: string; role: string }[];
  agents: { name: string; description: string; voiceId: string; language: string; systemPrompt: string; provider: string; model: string }[];
  workflows: { name: string; description: string; steps: object[] }[];
  contactCount: number;
  contactTags: string[];
  contactFieldGenerator: () => Record<string, string>;
  campaigns: { name: string; description: string; status: string; scheduledAt: Date | null; startedAt: Date | null; completedAt: Date | null }[];
  callCount: number;
  transcriptBuilders: (() => object[])[];
  phoneNumberCount: number;
  knowledgeDocs: { title: string; content: string; sourceUrl: string }[];
}

async function seedTenant(config: TenantSeedConfig) {
  // 1. Create tenant
  const tenant = await prisma.tenant.create({
    data: {
      name: config.name,
      slug: config.slug,
      industry: config.industry,
      isActive: true,
      settings: {
        timezone: 'America/New_York',
        businessHoursStart: '09:00',
        businessHoursEnd: '17:00',
        maxConcurrentCalls: config.plan === 'enterprise' ? 50 : config.plan === 'professional' ? 20 : 5,
        recordingEnabled: true,
        transcriptionEnabled: true,
      },
    },
  });

  console.log(`  Creating tenant: ${config.name} (${tenant.id})`);

  // 2. Create users
  const createdUsers = [];
  for (const u of config.users) {
    const passwordHash = await hashPassword('Demo1234!');
    const user = await prisma.user.create({
      data: {
        email: u.email,
        passwordHash,
        firstName: u.firstName,
        lastName: u.lastName,
        role: u.role,
        tenantId: tenant.id,
        isActive: true,
        emailVerified: true,
        lastLoginAt: daysAgo(Math.floor(Math.random() * 7)),
      },
    });
    createdUsers.push(user);
  }
  console.log(`    ✓ ${createdUsers.length} users created`);

  // 3. Create agents
  const createdAgents = [];
  for (const a of config.agents) {
    const agent = await prisma.agent.create({
      data: {
        name: a.name,
        description: a.description,
        tenantId: tenant.id,
        voiceId: a.voiceId,
        language: a.language,
        isActive: true,
        systemPrompt: a.systemPrompt,
        llmProvider: a.provider,
        llmModel: a.model,
        temperature: 0.7,
        maxTokens: 2048,
        interruptionSensitivity: 0.5,
        silenceTimeout: 5000,
        maxCallDuration: 600,
        createdById: createdUsers[0]!.id,
        settings: {
          greeting: `Hello, thank you for calling ${config.name}.`,
          endCallPhrases: ['goodbye', 'have a nice day', 'thank you'],
          transferNumber: null,
        },
      },
    });
    createdAgents.push(agent);
  }
  console.log(`    ✓ ${createdAgents.length} agents created`);

  // 4. Create workflows
  const createdWorkflows = [];
  for (const w of config.workflows) {
    const workflow = await prisma.workflow.create({
      data: {
        name: w.name,
        description: w.description,
        tenantId: tenant.id,
        isActive: true,
        version: 1,
        steps: w.steps as any,
        createdById: createdUsers[0]!.id,
      },
    });
    createdWorkflows.push(workflow);
  }
  console.log(`    ✓ ${createdWorkflows.length} workflows created`);

  // 5. Create contacts
  const firstNames = ['James', 'Mary', 'Robert', 'Patricia', 'John', 'Jennifer', 'Michael', 'Linda', 'David', 'Elizabeth', 'William', 'Barbara', 'Richard', 'Susan', 'Joseph', 'Jessica', 'Thomas', 'Sarah', 'Christopher', 'Karen', 'Charles', 'Lisa', 'Daniel', 'Nancy', 'Matthew', 'Betty', 'Anthony', 'Margaret', 'Mark', 'Sandra'];
  const lastNames = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson', 'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson'];

  const createdContacts = [];
  for (let i = 0; i < config.contactCount; i++) {
    const firstName = firstNames[i % firstNames.length]!;
    const lastName = lastNames[i % lastNames.length]!;
    const contact = await prisma.contact.create({
      data: {
        firstName,
        lastName,
        email: `${firstName.toLowerCase()}.${lastName.toLowerCase()}${i}@example.com`,
        phone: randomPhone(),
        tenantId: tenant.id,
        tags: [pickRandom(config.contactTags), pickRandom(config.contactTags)].filter((v, idx, a) => a.indexOf(v) === idx),
        customFields: config.contactFieldGenerator(),
        source: pickRandom(['IMPORT', 'MANUAL', 'API', 'WEBFORM']),
        isActive: true,
      },
    });
    createdContacts.push(contact);
  }
  console.log(`    ✓ ${createdContacts.length} contacts created`);

  // 6. Create phone numbers
  const createdPhoneNumbers = [];
  for (let i = 0; i < config.phoneNumberCount; i++) {
    const pn = await prisma.phoneNumber.create({
      data: {
        number: randomPhone(),
        tenantId: tenant.id,
        provider: pickRandom(['TWILIO', 'VONAGE', 'TELNYX']),
        label: `Line ${i + 1}`,
        isActive: true,
        capabilities: ['VOICE', 'SMS'],
      },
    });
    createdPhoneNumbers.push(pn);
  }
  console.log(`    ✓ ${createdPhoneNumbers.length} phone numbers created`);

  // 7. Create campaigns
  const createdCampaigns = [];
  for (const c of config.campaigns) {
    const campaign = await prisma.campaign.create({
      data: {
        name: c.name,
        description: c.description,
        tenantId: tenant.id,
        status: c.status,
        agentId: createdAgents[0]!.id,
        phoneNumberId: createdPhoneNumbers[0]!.id,
        scheduledAt: c.scheduledAt,
        startedAt: c.startedAt,
        completedAt: c.completedAt,
        contactIds: createdContacts.slice(0, Math.min(10, createdContacts.length)).map((ct) => ct.id),
        settings: {
          maxConcurrentCalls: 5,
          callsBetweenPause: 50,
          retryAttempts: 2,
          retryDelayMinutes: 60,
          callingHoursStart: '09:00',
          callingHoursEnd: '17:00',
          timezone: 'America/New_York',
        },
        createdById: createdUsers[0]!.id,
      },
    });
    createdCampaigns.push(campaign);
  }
  console.log(`    ✓ ${createdCampaigns.length} campaigns created`);

  // 8. Create calls
  const outcomes = ['APPOINTMENT_BOOKED', 'CALLBACK_REQUESTED', 'INFORMATION_PROVIDED', 'TRANSFERRED', 'NOT_INTERESTED', 'PROMISE_TO_PAY', 'DISPUTE_FILED', 'DEMO_SCHEDULED', 'QUALIFIED_LEAD', null];
  const createdCalls = [];
  for (let i = 0; i < config.callCount; i++) {
    const status = pickRandom([...CALL_STATUSES]);
    const direction = pickRandom([...CALL_DIRECTIONS]);
    const duration = status === 'COMPLETED' ? randomDuration() : status === 'VOICEMAIL' ? Math.floor(15 + Math.random() * 30) : 0;
    const transcriptBuilder = pickRandom(config.transcriptBuilders);
    const transcript = status === 'COMPLETED' ? transcriptBuilder() : null;
    const startedAt = hoursAgo(Math.floor(Math.random() * 720)); // up to 30 days ago
    const endedAt = new Date(startedAt.getTime() + duration * 1000);

    const call = await prisma.call.create({
      data: {
        tenantId: tenant.id,
        agentId: createdAgents[i % createdAgents.length]!.id,
        contactId: createdContacts[i % createdContacts.length]!.id,
        phoneNumberId: createdPhoneNumbers[i % createdPhoneNumbers.length]!.id,
        campaignId: createdCampaigns.length > 0 ? createdCampaigns[i % createdCampaigns.length]!.id : null,
        direction,
        status,
        duration,
        startedAt,
        endedAt: status === 'COMPLETED' || status === 'VOICEMAIL' ? endedAt : null,
        transcript: transcript as any,
        summary: status === 'COMPLETED' ? `Call with ${createdContacts[i % createdContacts.length]!.firstName} ${createdContacts[i % createdContacts.length]!.lastName}. ${pickRandom(['Successful outcome.', 'Follow-up needed.', 'Customer was satisfied.', 'Needs escalation.', 'Resolved on call.'])}` : null,
        outcome: status === 'COMPLETED' ? pickRandom(outcomes.filter(Boolean)) : null,
        sentiment: status === 'COMPLETED' ? pickRandom(['POSITIVE', 'NEUTRAL', 'NEGATIVE']) : null,
        recordingUrl: status === 'COMPLETED' ? `https://storage.voiceai.platform/recordings/${tenant.id}/${Date.now()}-${i}.wav` : null,
        cost: status === 'COMPLETED' ? parseFloat((0.02 + Math.random() * 0.15).toFixed(4)) : 0,
        metadata: {
          provider: 'twilio',
          callSid: `CA${Array.from({ length: 32 }, () => '0123456789abcdef'[Math.floor(Math.random() * 16)]).join('')}`,
        },
      },
    });
    createdCalls.push(call);
  }
  console.log(`    ✓ ${createdCalls.length} calls created`);

  // 9. Create knowledge base with documents
  const kb = await prisma.knowledgeBase.create({
    data: {
      name: `${config.name} Knowledge Base`,
      description: `Primary knowledge base for ${config.name}`,
      tenantId: tenant.id,
      isActive: true,
    },
  });

  for (const doc of config.knowledgeDocs) {
    await prisma.knowledgeDocument.create({
      data: {
        title: doc.title,
        content: doc.content,
        sourceUrl: doc.sourceUrl,
        knowledgeBaseId: kb.id,
        tenantId: tenant.id,
        status: 'PROCESSED',
        tokenCount: Math.floor(500 + Math.random() * 5000),
        chunkCount: Math.floor(5 + Math.random() * 20),
      },
    });
  }
  console.log(`    ✓ Knowledge base with ${config.knowledgeDocs.length} documents created`);

  // 10. Create subscription
  const planPrices: Record<string, number> = { starter: 49, professional: 149, enterprise: 499 };
  await prisma.subscription.create({
    data: {
      tenantId: tenant.id,
      plan: config.plan,
      status: 'ACTIVE',
      priceMonthly: planPrices[config.plan] || 149,
      currentPeriodStart: daysAgo(15),
      currentPeriodEnd: daysAgo(-15),
      cancelAtPeriodEnd: false,
      features: {
        maxAgents: config.plan === 'enterprise' ? 50 : config.plan === 'professional' ? 15 : 3,
        maxConcurrentCalls: config.plan === 'enterprise' ? 50 : config.plan === 'professional' ? 20 : 5,
        maxContactsPerMonth: config.plan === 'enterprise' ? 100000 : config.plan === 'professional' ? 25000 : 5000,
        recordingRetentionDays: config.plan === 'enterprise' ? 365 : config.plan === 'professional' ? 90 : 30,
        analyticsEnabled: config.plan !== 'starter',
        customIntegrations: config.plan === 'enterprise',
        prioritySupport: config.plan === 'enterprise',
      },
    },
  });
  console.log(`    ✓ Subscription (${config.plan}) created`);

  // 11. Create usage records for current month
  const usageTypes = ['CALLS_MADE', 'MINUTES_USED', 'CONTACTS_IMPORTED', 'AI_TOKENS_USED', 'RECORDINGS_STORED_MB'];
  for (const usageType of usageTypes) {
    await prisma.usageRecord.create({
      data: {
        tenantId: tenant.id,
        type: usageType,
        quantity: Math.floor(100 + Math.random() * 5000),
        periodStart: daysAgo(30),
        periodEnd: new Date(),
        metadata: { billingCycle: 'monthly' },
      },
    });
  }
  console.log('    ✓ Usage records created');

  // 12. Create audit logs
  const auditActions = [
    { action: 'USER_LOGIN', resource: 'auth', description: 'User logged in successfully' },
    { action: 'AGENT_CREATED', resource: 'agent', description: 'New AI agent created' },
    { action: 'CAMPAIGN_STARTED', resource: 'campaign', description: 'Campaign execution started' },
    { action: 'CONTACT_IMPORTED', resource: 'contact', description: 'Contacts imported via CSV' },
    { action: 'SETTINGS_UPDATED', resource: 'tenant', description: 'Tenant settings updated' },
    { action: 'WORKFLOW_PUBLISHED', resource: 'workflow', description: 'Workflow published to production' },
    { action: 'PHONE_NUMBER_ADDED', resource: 'phone_number', description: 'New phone number provisioned' },
    { action: 'KNOWLEDGE_DOC_UPLOADED', resource: 'knowledge_base', description: 'Knowledge document uploaded and processed' },
    { action: 'USER_ROLE_CHANGED', resource: 'user', description: 'User role updated' },
    { action: 'BILLING_PLAN_UPGRADED', resource: 'billing', description: 'Subscription plan upgraded' },
  ];

  for (const audit of auditActions) {
    await prisma.auditLog.create({
      data: {
        tenantId: tenant.id,
        userId: pickRandom(createdUsers).id,
        action: audit.action,
        resource: audit.resource,
        description: audit.description,
        ipAddress: `192.168.${Math.floor(Math.random() * 255)}.${Math.floor(Math.random() * 255)}`,
        userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        metadata: {},
        createdAt: hoursAgo(Math.floor(Math.random() * 720)),
      },
    });
  }
  console.log('    ✓ 10 audit logs created');

  // 13. Create notifications
  const notificationTypes = [
    { type: 'CAMPAIGN_COMPLETED', title: 'Campaign Completed', body: `Your campaign has finished. ${Math.floor(80 + Math.random() * 20)}% of contacts were reached.` },
    { type: 'CALL_ESCALATION', title: 'Call Escalation Required', body: 'A call has been flagged for manual review due to negative sentiment.' },
    { type: 'USAGE_WARNING', title: 'Usage Threshold Alert', body: 'You have used 80% of your monthly call minutes allocation.' },
    { type: 'SYSTEM_UPDATE', title: 'Platform Update', body: 'New features are available: improved sentiment analysis and call recording playback.' },
    { type: 'PAYMENT_SUCCESS', title: 'Payment Processed', body: 'Your monthly subscription payment has been processed successfully.' },
  ];

  for (const notif of notificationTypes) {
    await prisma.notification.create({
      data: {
        tenantId: tenant.id,
        userId: createdUsers[0]!.id,
        type: notif.type,
        title: notif.title,
        body: notif.body,
        isRead: Math.random() > 0.5,
        createdAt: hoursAgo(Math.floor(Math.random() * 168)),
      },
    });
  }
  console.log('    ✓ 5 notifications created');

  return tenant;
}

// ─── Main ──────────────────────────────────────────────────────────────────

async function main() {
  console.log('🌱 Starting Voice AI Platform seed...\n');

  // Feature flags
  await seedFeatureFlags();

  // Super admin
  await seedSuperAdmin();

  // ──── Tenant 1: MedCare Health Systems ────────────────────────────────
  console.log('\n📋 Tenant 1: MedCare Health Systems');
  await seedTenant({
    name: 'MedCare Health Systems',
    slug: 'medcare-health',
    industry: 'HEALTHCARE',
    plan: 'professional',
    users: [
      { email: 'dr.admin@medcare.example.com', firstName: 'Dr. Robert', lastName: 'Chen', role: 'TENANT_OWNER' },
      { email: 'sarah.nurse@medcare.example.com', firstName: 'Sarah', lastName: 'Mitchell', role: 'MANAGER' },
      { email: 'mike.reception@medcare.example.com', firstName: 'Mike', lastName: 'Torres', role: 'AGENT_OPERATOR' },
    ],
    agents: [
      {
        name: 'Appointment Booking Agent',
        description: 'Handles scheduling, rescheduling, and cancellation of patient appointments.',
        voiceId: 'alloy',
        language: 'en-US',
        systemPrompt: 'You are a friendly and professional medical office receptionist. Help patients schedule, reschedule, or cancel appointments. Always confirm patient identity before making changes. Maintain HIPAA compliance at all times.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Patient Follow-up Agent',
        description: 'Conducts post-visit follow-up calls to check on patient recovery and medication adherence.',
        voiceId: 'nova',
        language: 'en-US',
        systemPrompt: 'You are a caring healthcare follow-up coordinator. Call patients after their appointments to check on recovery, medication side effects, and schedule follow-ups. Be empathetic and thorough.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Lab Results Notifier',
        description: 'Notifies patients when lab results are available and schedules follow-up if needed.',
        voiceId: 'shimmer',
        language: 'en-US',
        systemPrompt: 'You are a medical lab results coordinator. Inform patients that their lab results are available and guide them on next steps. Never disclose specific results over the phone — direct them to the patient portal or schedule a doctor consultation.',
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
      },
    ],
    workflows: [
      {
        name: 'Appointment Booking Flow',
        description: 'Multi-step appointment scheduling workflow',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Hello, thank you for calling MedCare Health Systems.' } },
          { id: 'identify', type: 'COLLECT', config: { field: 'patient_id', prompt: 'May I have your patient ID or date of birth?' } },
          { id: 'lookup', type: 'API_CALL', config: { endpoint: '/api/patients/lookup', method: 'POST' } },
          { id: 'schedule', type: 'COLLECT', config: { field: 'preferred_time', prompt: 'What date and time works best for you?' } },
          { id: 'confirm', type: 'SPEAK', config: { text: 'I have booked your appointment. You will receive a confirmation text shortly.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
      {
        name: 'Follow-up Call Flow',
        description: 'Post-visit patient check-in workflow',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Hello, this is MedCare Health Systems calling for a follow-up.' } },
          { id: 'check_condition', type: 'COLLECT', config: { field: 'feeling', prompt: 'How are you feeling since your last visit?' } },
          { id: 'check_medication', type: 'COLLECT', config: { field: 'medication_issues', prompt: 'Have you experienced any side effects from your medication?' } },
          { id: 'branch', type: 'CONDITION', config: { field: 'medication_issues', condition: 'contains', value: 'yes', trueStep: 'escalate', falseStep: 'confirm_followup' } },
          { id: 'escalate', type: 'TRANSFER', config: { target: 'nurse_line', message: 'Transferring you to our nurse for immediate assistance.' } },
          { id: 'confirm_followup', type: 'SPEAK', config: { text: 'Great to hear. Your next check-up is on file. Have a good day!' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
    ],
    contactCount: 20,
    contactTags: ['new-patient', 'follow-up-needed', 'chronic-care', 'pediatric', 'insurance-verified', 'wellness-check-due'],
    contactFieldGenerator: () => ({
      insuranceProvider: pickRandom(['Aetna', 'BlueCross', 'UnitedHealth', 'Cigna', 'Medicare']),
      primaryDoctor: pickRandom(['Dr. Chen', 'Dr. Patel', 'Dr. Kim', 'Dr. Johnson']),
      lastVisit: daysAgo(Math.floor(Math.random() * 90)).toISOString().split('T')[0]!,
    }),
    campaigns: [
      {
        name: 'Q1 Wellness Check Reminders',
        description: 'Automated calls to patients due for annual wellness checks in Q1 2026.',
        status: 'IN_PROGRESS',
        scheduledAt: daysAgo(5),
        startedAt: daysAgo(4),
        completedAt: null,
      },
    ],
    callCount: 15,
    transcriptBuilders: [buildHealthcareAppointmentTranscript, buildHealthcareFollowUpTranscript],
    phoneNumberCount: 2,
    knowledgeDocs: [
      { title: 'HIPAA Compliance Guide', content: 'Comprehensive guide to HIPAA regulations for patient communication...', sourceUrl: 'https://docs.medcare.example.com/hipaa-guide' },
      { title: 'Appointment Scheduling Policies', content: 'Office policies for scheduling, cancellation (24hr notice), and no-show procedures...', sourceUrl: 'https://docs.medcare.example.com/scheduling-policies' },
      { title: 'Insurance Verification Procedures', content: 'Step-by-step insurance verification process including pre-authorization requirements...', sourceUrl: 'https://docs.medcare.example.com/insurance-verification' },
    ],
  });

  // ──── Tenant 2: FinanceFirst Collections ──────────────────────────────
  console.log('\n📋 Tenant 2: FinanceFirst Collections');
  await seedTenant({
    name: 'FinanceFirst Collections',
    slug: 'financefirst-collections',
    industry: 'COLLECTIONS',
    plan: 'enterprise',
    users: [
      { email: 'manager@financefirst.example.com', firstName: 'Angela', lastName: 'Brooks', role: 'TENANT_OWNER' },
      { email: 'teamlead@financefirst.example.com', firstName: 'Derek', lastName: 'Watkins', role: 'SUPERVISOR' },
      { email: 'collector@financefirst.example.com', firstName: 'Priya', lastName: 'Sharma', role: 'AGENT_OPERATOR' },
    ],
    agents: [
      {
        name: 'Payment Reminder Agent',
        description: 'Makes automated payment reminder calls for overdue accounts.',
        voiceId: 'echo',
        language: 'en-US',
        systemPrompt: 'You are a professional but empathetic debt collection agent. Remind debtors of overdue balances, offer payment plan options, and document promises to pay. Always comply with FDCPA regulations. Never threaten or harass.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Promise-to-Pay Agent',
        description: 'Handles follow-up calls for accounts with existing payment commitments.',
        voiceId: 'fable',
        language: 'en-US',
        systemPrompt: 'You are a collections follow-up specialist. Contact debtors who have made payment promises. Confirm payment arrangements, offer alternative plans if needed, and maintain a professional tone.',
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
      },
      {
        name: 'Dispute Handler Agent',
        description: 'Manages account disputes and routes to appropriate resolution teams.',
        voiceId: 'onyx',
        language: 'en-US',
        systemPrompt: 'You are a dispute resolution specialist. Listen to debtor disputes, collect evidence details (tracking numbers, receipts), and document the dispute for investigation. Be impartial and thorough.',
        provider: 'openai',
        model: 'gpt-4o',
      },
    ],
    workflows: [
      {
        name: 'Payment Reminder Flow',
        description: 'Automated payment reminder with escalation path',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Hello, this is FinanceFirst Collections calling regarding your account.' } },
          { id: 'verify', type: 'COLLECT', config: { field: 'identity_confirmed', prompt: 'For verification, may I have the last four digits of your SSN?' } },
          { id: 'state_balance', type: 'SPEAK', config: { text: 'Our records show an outstanding balance on your account.' } },
          { id: 'offer_options', type: 'COLLECT', config: { field: 'payment_choice', prompt: 'Would you like to pay in full today, set up a payment plan, or discuss other options?' } },
          { id: 'branch', type: 'CONDITION', config: { field: 'payment_choice', condition: 'contains', value: 'plan', trueStep: 'setup_plan', falseStep: 'process_payment' } },
          { id: 'setup_plan', type: 'API_CALL', config: { endpoint: '/api/payment-plans', method: 'POST' } },
          { id: 'process_payment', type: 'API_CALL', config: { endpoint: '/api/payments', method: 'POST' } },
          { id: 'confirm', type: 'SPEAK', config: { text: 'Thank you. You will receive confirmation via email.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
      {
        name: 'Dispute Resolution Flow',
        description: 'Handles incoming disputes with documentation',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'FinanceFirst Collections dispute line. How can I help?' } },
          { id: 'collect_dispute', type: 'COLLECT', config: { field: 'dispute_reason', prompt: 'Please describe the reason for your dispute.' } },
          { id: 'collect_evidence', type: 'COLLECT', config: { field: 'evidence', prompt: 'Do you have any documentation such as tracking numbers or receipts?' } },
          { id: 'create_case', type: 'API_CALL', config: { endpoint: '/api/disputes', method: 'POST' } },
          { id: 'confirm', type: 'SPEAK', config: { text: 'Your dispute has been filed and your account is on hold. We will contact you within 5 business days.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
    ],
    contactCount: 25,
    contactTags: ['overdue-30', 'overdue-60', 'overdue-90', 'promise-to-pay', 'dispute-pending', 'payment-plan', 'high-balance', 'first-notice'],
    contactFieldGenerator: () => ({
      accountNumber: `ACC-${Math.floor(10000 + Math.random() * 90000)}`,
      outstandingBalance: `$${(500 + Math.random() * 9500).toFixed(2)}`,
      daysOverdue: `${Math.floor(15 + Math.random() * 120)}`,
      originalCreditor: pickRandom(['National Bank', 'Credit Union One', 'Metro Finance', 'Pacific Lending', 'Atlantic Credit']),
    }),
    campaigns: [
      {
        name: 'March Overdue Accounts',
        description: 'Outreach to all accounts 30+ days overdue as of March 2026.',
        status: 'ACTIVE',
        scheduledAt: daysAgo(3),
        startedAt: daysAgo(2),
        completedAt: null,
      },
    ],
    callCount: 20,
    transcriptBuilders: [buildCollectionsPaymentTranscript, buildCollectionsDisputeTranscript],
    phoneNumberCount: 3,
    knowledgeDocs: [
      { title: 'FDCPA Compliance Manual', content: 'Fair Debt Collection Practices Act compliance guidelines for all agents...', sourceUrl: 'https://docs.financefirst.example.com/fdcpa' },
      { title: 'Payment Plan Calculator Guide', content: 'How to calculate and offer appropriate payment plans based on balance and debtor situation...', sourceUrl: 'https://docs.financefirst.example.com/payment-plans' },
    ],
  });

  // ──── Tenant 3: LegalEagle Partners ───────────────────────────────────
  console.log('\n📋 Tenant 3: LegalEagle Partners');
  await seedTenant({
    name: 'LegalEagle Partners',
    slug: 'legaleagle-partners',
    industry: 'LEGAL',
    plan: 'professional',
    users: [
      { email: 'managing.partner@legaleagle.example.com', firstName: 'Victoria', lastName: 'Reeves', role: 'TENANT_OWNER' },
      { email: 'assistant@legaleagle.example.com', firstName: 'Jordan', lastName: 'Blake', role: 'MANAGER' },
      { email: 'intake@legaleagle.example.com', firstName: 'Alex', lastName: 'Rivera', role: 'AGENT_OPERATOR' },
    ],
    agents: [
      {
        name: 'Client Intake Agent',
        description: 'Handles initial client intake calls, collects case information, and schedules consultations.',
        voiceId: 'nova',
        language: 'en-US',
        systemPrompt: 'You are a professional legal intake specialist. Collect initial case information from prospective clients, determine the type of legal matter, and schedule consultations. Be empathetic but maintain professional boundaries. Never provide legal advice.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Consultation Booking Agent',
        description: 'Manages attorney consultation scheduling.',
        voiceId: 'alloy',
        language: 'en-US',
        systemPrompt: 'You are a legal office scheduling assistant. Help clients book consultations with the appropriate attorney based on their case type. Manage the attorneys calendar efficiently.',
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
      },
      {
        name: 'Case Status Agent',
        description: 'Provides case status updates to existing clients.',
        voiceId: 'shimmer',
        language: 'en-US',
        systemPrompt: 'You are a legal case status coordinator. Provide clients with updates on their case progress. Be clear about timelines and next steps. Never speculate on case outcomes.',
        provider: 'openai',
        model: 'gpt-4o',
      },
    ],
    workflows: [
      {
        name: 'New Client Intake Flow',
        description: 'Comprehensive new client intake process',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Thank you for calling LegalEagle Partners. How may I direct your call?' } },
          { id: 'case_type', type: 'COLLECT', config: { field: 'case_type', prompt: 'Could you briefly describe your legal matter?' } },
          { id: 'classify', type: 'AI_CLASSIFY', config: { categories: ['personal_injury', 'family_law', 'criminal_defense', 'business_law', 'other'] } },
          { id: 'collect_details', type: 'COLLECT', config: { field: 'incident_details', prompt: 'Can you provide more details about when and how this happened?' } },
          { id: 'check_conflict', type: 'API_CALL', config: { endpoint: '/api/conflict-check', method: 'POST' } },
          { id: 'schedule', type: 'COLLECT', config: { field: 'consultation_time', prompt: 'I would like to schedule a free consultation. What day and time works best?' } },
          { id: 'confirm', type: 'SPEAK', config: { text: 'Your consultation has been booked. We will send a confirmation email with pre-consultation forms.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
    ],
    contactCount: 15,
    contactTags: ['personal-injury', 'family-law', 'criminal-defense', 'consultation-scheduled', 'high-priority', 'referral'],
    contactFieldGenerator: () => ({
      caseType: pickRandom(['Personal Injury', 'Family Law', 'Criminal Defense', 'Business Litigation', 'Estate Planning']),
      referralSource: pickRandom(['Google', 'Referral', 'Bar Association', 'Previous Client', 'Social Media']),
      urgency: pickRandom(['HIGH', 'MEDIUM', 'LOW']),
    }),
    campaigns: [
      {
        name: 'Personal Injury Lead Follow-up',
        description: 'Follow up on personal injury leads from web forms and referrals within 24 hours.',
        status: 'ACTIVE',
        scheduledAt: daysAgo(1),
        startedAt: daysAgo(1),
        completedAt: null,
      },
    ],
    callCount: 10,
    transcriptBuilders: [buildLegalIntakeTranscript],
    phoneNumberCount: 1,
    knowledgeDocs: [
      { title: 'Client Intake Procedures', content: 'Standard intake procedures for different case types including required information and conflict checks...', sourceUrl: 'https://docs.legaleagle.example.com/intake' },
      { title: 'Consultation Guidelines', content: 'Attorney consultation scheduling rules, duration by case type, and follow-up procedures...', sourceUrl: 'https://docs.legaleagle.example.com/consultations' },
      { title: 'Legal Ethics Quick Reference', content: 'Quick reference for ethical obligations during client communications, including confidentiality and conflict of interest...', sourceUrl: 'https://docs.legaleagle.example.com/ethics' },
    ],
  });

  // ──── Tenant 4: SalesForce Elite ──────────────────────────────────────
  console.log('\n📋 Tenant 4: SalesForce Elite');
  await seedTenant({
    name: 'SalesForce Elite',
    slug: 'salesforce-elite',
    industry: 'SALES',
    plan: 'enterprise',
    users: [
      { email: 'vp.sales@saleselite.example.com', firstName: 'Marcus', lastName: 'Williams', role: 'TENANT_OWNER' },
      { email: 'sales.manager@saleselite.example.com', firstName: 'Rachel', lastName: 'Kim', role: 'MANAGER' },
      { email: 'sdr@saleselite.example.com', firstName: 'Tyler', lastName: 'Rodriguez', role: 'AGENT_OPERATOR' },
      { email: 'qa@saleselite.example.com', firstName: 'Megan', lastName: 'Patel', role: 'QA_AUDITOR' },
    ],
    agents: [
      {
        name: 'Lead Qualifier Agent',
        description: 'Qualifies inbound and outbound leads based on BANT criteria.',
        voiceId: 'echo',
        language: 'en-US',
        systemPrompt: 'You are a professional sales development representative. Qualify leads using BANT criteria (Budget, Authority, Need, Timeline). Be enthusiastic but not pushy. Focus on understanding the prospects pain points and decision-making process.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Demo Booking Agent',
        description: 'Handles scheduling product demos with qualified prospects.',
        voiceId: 'alloy',
        language: 'en-US',
        systemPrompt: 'You are a product demo coordinator. Schedule personalized demos based on the prospects industry and team size. Highlight relevant features and prepare expectations for the demo call.',
        provider: 'openai',
        model: 'gpt-4o',
      },
      {
        name: 'Win-back Agent',
        description: 'Re-engages churned customers with tailored offers and product updates.',
        voiceId: 'nova',
        language: 'en-US',
        systemPrompt: 'You are a customer win-back specialist. Reach out to churned customers to understand why they left, share new features and improvements, and offer incentives to return. Be understanding and never pressure.',
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
      },
    ],
    workflows: [
      {
        name: 'Lead Qualification Flow',
        description: 'BANT-based lead qualification workflow',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Hi, this is SalesForce Elite! Thanks for your interest in our platform.' } },
          { id: 'budget', type: 'COLLECT', config: { field: 'budget_range', prompt: 'To best help you, could you share your approximate budget for a solution like this?' } },
          { id: 'authority', type: 'COLLECT', config: { field: 'decision_maker', prompt: 'Are you the primary decision maker, or will others be involved?' } },
          { id: 'need', type: 'COLLECT', config: { field: 'pain_points', prompt: 'What are the biggest challenges with your current solution?' } },
          { id: 'timeline', type: 'COLLECT', config: { field: 'timeline', prompt: 'What is your timeline for implementing a new solution?' } },
          { id: 'score', type: 'AI_SCORE', config: { model: 'lead_qualification', threshold: 70 } },
          { id: 'branch', type: 'CONDITION', config: { field: 'score', condition: 'gte', value: 70, trueStep: 'book_demo', falseStep: 'nurture' } },
          { id: 'book_demo', type: 'COLLECT', config: { field: 'demo_time', prompt: 'Great! Let me schedule a personalized demo. What day works best?' } },
          { id: 'nurture', type: 'SPEAK', config: { text: 'Thank you for sharing. I will send over some resources that might be helpful.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
      {
        name: 'Demo Booking Flow',
        description: 'Streamlined demo scheduling workflow',
        steps: [
          { id: 'greet', type: 'SPEAK', config: { text: 'Thanks for your interest in a demo of our platform!' } },
          { id: 'team_size', type: 'COLLECT', config: { field: 'team_size', prompt: 'How large is your sales team?' } },
          { id: 'current_tools', type: 'COLLECT', config: { field: 'current_tools', prompt: 'What tools are you currently using?' } },
          { id: 'schedule', type: 'COLLECT', config: { field: 'preferred_time', prompt: 'When would you like to schedule the demo?' } },
          { id: 'confirm', type: 'SPEAK', config: { text: 'Your demo is booked! You will receive a calendar invite and a pre-demo questionnaire shortly.' } },
          { id: 'end', type: 'END_CALL', config: {} },
        ],
      },
    ],
    contactCount: 30,
    contactTags: ['hot-lead', 'warm-lead', 'cold-lead', 'demo-requested', 'demo-completed', 'proposal-sent', 'churned', 'enterprise', 'mid-market', 'startup'],
    contactFieldGenerator: () => ({
      company: pickRandom(['TechCorp Inc', 'Global Solutions Ltd', 'Innovate AI', 'DataDriven Co', 'CloudScale Systems', 'Nexus Enterprises', 'Velocity Partners', 'Apex Digital', 'Summit Software', 'Pinnacle Group']),
      title: pickRandom(['VP Sales', 'Sales Director', 'Head of Revenue', 'CRO', 'Sales Manager', 'VP Business Development', 'Director of Operations', 'CEO']),
      teamSize: `${Math.floor(5 + Math.random() * 200)}`,
      annualRevenue: pickRandom(['$1M-$5M', '$5M-$20M', '$20M-$50M', '$50M-$100M', '$100M+']),
      leadScore: `${Math.floor(20 + Math.random() * 80)}`,
    }),
    campaigns: [
      {
        name: 'Enterprise Lead Qualification Q1',
        description: 'Qualify and schedule demos with enterprise leads from Q1 marketing campaigns.',
        status: 'IN_PROGRESS',
        scheduledAt: daysAgo(10),
        startedAt: daysAgo(9),
        completedAt: null,
      },
      {
        name: 'Win-back Churned Accounts',
        description: 'Re-engage accounts that churned in the last 90 days with new feature announcements.',
        status: 'SCHEDULED',
        scheduledAt: daysAgo(-3),
        startedAt: null,
        completedAt: null,
      },
    ],
    callCount: 25,
    transcriptBuilders: [buildSalesQualificationTranscript, buildSalesDemoBookingTranscript],
    phoneNumberCount: 4,
    knowledgeDocs: [
      { title: 'Sales Playbook', content: 'Comprehensive sales playbook including objection handling, competitive positioning, and closing techniques...', sourceUrl: 'https://docs.saleselite.example.com/playbook' },
      { title: 'Product Feature Guide', content: 'Complete feature list with benefits, use cases, and competitive differentiators for each plan tier...', sourceUrl: 'https://docs.saleselite.example.com/features' },
    ],
  });

  console.log('\n✅ Seed completed successfully!');
  console.log('\n📊 Summary:');
  console.log('  - 1 super admin');
  console.log('  - 4 tenants (Healthcare, Collections, Legal, Sales)');
  console.log('  - 13 users across tenants');
  console.log('  - 12 AI agents');
  console.log('  - 7 workflows');
  console.log('  - 90 contacts');
  console.log('  - 70 calls with transcripts');
  console.log('  - 5 campaigns');
  console.log('  - 10 phone numbers');
  console.log('  - 4 knowledge bases with 10 documents');
  console.log('  - 4 subscriptions');
  console.log('  - 20 usage records');
  console.log('  - 40 audit logs');
  console.log('  - 20 notifications');
  console.log('  - 5 feature flags');
}

main()
  .catch((e) => {
    console.error('❌ Seed failed:', e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
