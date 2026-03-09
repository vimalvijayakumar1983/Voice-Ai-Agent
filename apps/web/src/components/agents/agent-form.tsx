'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AGENT_TYPES, INDUSTRIES, VOICE_OPTIONS, LANGUAGES } from '@/lib/constants';
import { Check, ChevronLeft, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

const agentSchema = z.object({
  name: z.string().min(1, 'Agent name is required'),
  type: z.string().min(1, 'Agent type is required'),
  industry: z.string().min(1, 'Industry is required'),
  description: z.string().optional(),
  voiceId: z.string().min(1, 'Voice is required'),
  language: z.string().min(1, 'Language is required'),
  speakingRate: z.number().min(0.5).max(2.0),
  personality: z.string().optional(),
  tone: z.string().optional(),
  systemPrompt: z.string().min(1, 'System prompt is required'),
  knowledgeBase: z.string().optional(),
  maxCallDuration: z.number().min(30).max(3600),
  escalationRules: z.string().optional(),
  complianceNotes: z.string().optional(),
});

type AgentFormData = z.infer<typeof agentSchema>;

const steps = [
  { id: 'basic', title: 'Basic Info', description: 'Name, type, and industry' },
  { id: 'voice', title: 'Voice & Language', description: 'Voice selection and language' },
  { id: 'personality', title: 'Personality & Tone', description: 'Agent personality' },
  { id: 'prompt', title: 'Prompt & Knowledge', description: 'System prompt and knowledge base' },
  { id: 'rules', title: 'Rules & Escalation', description: 'Call rules and escalation paths' },
  { id: 'review', title: 'Review', description: 'Review and create' },
];

export function AgentForm() {
  const [currentStep, setCurrentStep] = useState(0);

  const form = useForm<AgentFormData>({
    resolver: zodResolver(agentSchema),
    defaultValues: {
      name: '',
      type: '',
      industry: '',
      description: '',
      voiceId: '',
      language: 'en-US',
      speakingRate: 1.0,
      personality: '',
      tone: '',
      systemPrompt: '',
      knowledgeBase: '',
      maxCallDuration: 300,
      escalationRules: '',
      complianceNotes: '',
    },
  });

  const onSubmit = (data: AgentFormData) => {
    console.log('Agent form submitted:', data);
  };

  const nextStep = () => {
    if (currentStep < steps.length - 1) setCurrentStep(currentStep + 1);
  };
  const prevStep = () => {
    if (currentStep > 0) setCurrentStep(currentStep - 1);
  };

  return (
    <div className="space-y-6">
      {/* Steps indicator */}
      <nav className="flex items-center justify-center">
        <ol className="flex items-center gap-2">
          {steps.map((step, index) => (
            <li key={step.id} className="flex items-center">
              <button
                onClick={() => setCurrentStep(index)}
                className={cn(
                  'flex items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors',
                  index === currentStep
                    ? 'bg-primary text-primary-foreground'
                    : index < currentStep
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:bg-muted'
                )}
              >
                <span
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full text-xs font-medium',
                    index < currentStep && 'bg-primary text-primary-foreground'
                  )}
                >
                  {index < currentStep ? <Check className="h-3.5 w-3.5" /> : index + 1}
                </span>
                <span className="hidden md:inline">{step.title}</span>
              </button>
              {index < steps.length - 1 && (
                <div className={cn('mx-1 h-px w-8', index < currentStep ? 'bg-primary' : 'bg-border')} />
              )}
            </li>
          ))}
        </ol>
      </nav>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)}>
          {/* Step 0: Basic Info */}
          {currentStep === 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Basic Information</CardTitle>
                <CardDescription>Configure the fundamental settings for your AI agent.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <FormField
                  control={form.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Agent Name</FormLabel>
                      <FormControl>
                        <Input placeholder="e.g., Sales Outreach Agent" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="type"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Agent Type</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select agent type" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {Object.entries(AGENT_TYPES).map(([key, value]) => (
                            <SelectItem key={value} value={value}>
                              {key.replace(/_/g, ' ')}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="industry"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Industry</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select industry" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {INDUSTRIES.map((industry) => (
                            <SelectItem key={industry} value={industry}>
                              {industry}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="description"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Description</FormLabel>
                      <FormControl>
                        <Textarea placeholder="Briefly describe what this agent does..." {...field} />
                      </FormControl>
                      <FormDescription>Optional description for internal reference.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </CardContent>
            </Card>
          )}

          {/* Step 1: Voice & Language */}
          {currentStep === 1 && (
            <Card>
              <CardHeader>
                <CardTitle>Voice & Language</CardTitle>
                <CardDescription>Choose the voice and language for your agent.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <FormField
                  control={form.control}
                  name="voiceId"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Voice</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select a voice" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {VOICE_OPTIONS.map((voice) => (
                            <SelectItem key={voice.id} value={voice.id}>
                              {voice.name} - {voice.gender}, {voice.accent}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="language"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Language</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select language" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {LANGUAGES.map((lang) => (
                            <SelectItem key={lang.code} value={lang.code}>
                              {lang.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="speakingRate"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Speaking Rate: {field.value}x</FormLabel>
                      <FormControl>
                        <input
                          type="range"
                          min="0.5"
                          max="2.0"
                          step="0.1"
                          value={field.value}
                          onChange={(e) => field.onChange(parseFloat(e.target.value))}
                          className="w-full"
                        />
                      </FormControl>
                      <FormDescription>Adjust how fast the agent speaks (0.5x - 2.0x).</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </CardContent>
            </Card>
          )}

          {/* Step 2: Personality & Tone */}
          {currentStep === 2 && (
            <Card>
              <CardHeader>
                <CardTitle>Personality & Tone</CardTitle>
                <CardDescription>Define how your agent communicates.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <FormField
                  control={form.control}
                  name="personality"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Personality Traits</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="e.g., Friendly, professional, patient, empathetic..."
                          className="min-h-[100px]"
                          {...field}
                        />
                      </FormControl>
                      <FormDescription>Describe the personality traits your agent should exhibit.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="tone"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Communication Tone</FormLabel>
                      <Select onValueChange={field.onChange} defaultValue={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select tone" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          <SelectItem value="professional">Professional</SelectItem>
                          <SelectItem value="friendly">Friendly</SelectItem>
                          <SelectItem value="casual">Casual</SelectItem>
                          <SelectItem value="formal">Formal</SelectItem>
                          <SelectItem value="empathetic">Empathetic</SelectItem>
                          <SelectItem value="assertive">Assertive</SelectItem>
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </CardContent>
            </Card>
          )}

          {/* Step 3: Prompt & Knowledge */}
          {currentStep === 3 && (
            <Card>
              <CardHeader>
                <CardTitle>Prompt & Knowledge Base</CardTitle>
                <CardDescription>Configure the system prompt and knowledge your agent uses.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <FormField
                  control={form.control}
                  name="systemPrompt"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>System Prompt</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="You are a helpful sales agent for [Company]. Your goal is to..."
                          className="min-h-[200px] font-mono text-sm"
                          {...field}
                        />
                      </FormControl>
                      <FormDescription>The core instruction set for your AI agent.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="knowledgeBase"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Knowledge Base</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="Add product details, FAQs, pricing info..."
                          className="min-h-[150px]"
                          {...field}
                        />
                      </FormControl>
                      <FormDescription>Additional context and information the agent can reference.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </CardContent>
            </Card>
          )}

          {/* Step 4: Rules & Escalation */}
          {currentStep === 4 && (
            <Card>
              <CardHeader>
                <CardTitle>Rules & Escalation</CardTitle>
                <CardDescription>Set call duration limits and escalation policies.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <FormField
                  control={form.control}
                  name="maxCallDuration"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Max Call Duration (seconds)</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={30}
                          max={3600}
                          {...field}
                          onChange={(e) => field.onChange(parseInt(e.target.value))}
                        />
                      </FormControl>
                      <FormDescription>Maximum call length before the agent wraps up.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="escalationRules"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Escalation Rules</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="Define when and how to escalate to a human agent..."
                          className="min-h-[120px]"
                          {...field}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="complianceNotes"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Compliance Notes</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="Required disclosures, consent scripts, do-not-say phrases..."
                          className="min-h-[100px]"
                          {...field}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </CardContent>
            </Card>
          )}

          {/* Step 5: Review */}
          {currentStep === 5 && (
            <Card>
              <CardHeader>
                <CardTitle>Review Configuration</CardTitle>
                <CardDescription>Review your agent configuration before creating.</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  {[
                    { label: 'Name', value: form.getValues('name') },
                    { label: 'Type', value: form.getValues('type') },
                    { label: 'Industry', value: form.getValues('industry') },
                    { label: 'Voice', value: form.getValues('voiceId') },
                    { label: 'Language', value: form.getValues('language') },
                    { label: 'Max Duration', value: `${form.getValues('maxCallDuration')}s` },
                  ].map((item) => (
                    <div key={item.label} className="flex items-center justify-between border-b pb-3">
                      <span className="text-sm text-muted-foreground">{item.label}</span>
                      <span className="text-sm font-medium">{item.value || 'Not set'}</span>
                    </div>
                  ))}
                  {form.getValues('systemPrompt') && (
                    <div>
                      <span className="text-sm text-muted-foreground">System Prompt</span>
                      <p className="mt-1 rounded-md bg-muted p-3 text-sm">
                        {form.getValues('systemPrompt').slice(0, 200)}
                        {form.getValues('systemPrompt').length > 200 ? '...' : ''}
                      </p>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Navigation buttons */}
          <div className="flex items-center justify-between pt-4">
            <Button
              type="button"
              variant="outline"
              onClick={prevStep}
              disabled={currentStep === 0}
            >
              <ChevronLeft className="mr-2 h-4 w-4" />
              Previous
            </Button>
            {currentStep < steps.length - 1 ? (
              <Button type="button" onClick={nextStep}>
                Next
                <ChevronRight className="ml-2 h-4 w-4" />
              </Button>
            ) : (
              <Button type="submit">Create Agent</Button>
            )}
          </div>
        </form>
      </Form>
    </div>
  );
}
