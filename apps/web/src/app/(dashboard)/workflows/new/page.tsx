'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { GitBranch, Code, Save } from 'lucide-react';

export default function NewWorkflowPage() {
  const [workflowJson, setWorkflowJson] = useState(
    JSON.stringify(
      {
        name: '',
        nodes: [
          { id: 'start', type: 'trigger', config: { event: 'call.started' } },
          { id: 'greet', type: 'action', config: { action: 'speak', text: 'Hello!' } },
          { id: 'end', type: 'end', config: {} },
        ],
        edges: [
          { from: 'start', to: 'greet' },
          { from: 'greet', to: 'end' },
        ],
      },
      null,
      2
    )
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Create Workflow</h2>
          <p className="text-sm text-muted-foreground">
            Build automated call flows and actions
          </p>
        </div>
        <Button>
          <Save className="mr-2 h-4 w-4" />
          Save Workflow
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Workflow Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input placeholder="My Workflow" />
              </div>
              <div className="space-y-2">
                <Label>Description</Label>
                <Textarea placeholder="Describe what this workflow does..." />
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="lg:col-span-2">
          <Tabs defaultValue="visual">
            <TabsList>
              <TabsTrigger value="visual">
                <GitBranch className="mr-2 h-4 w-4" />
                Visual Editor
              </TabsTrigger>
              <TabsTrigger value="json">
                <Code className="mr-2 h-4 w-4" />
                JSON Editor
              </TabsTrigger>
            </TabsList>

            <TabsContent value="visual">
              <Card>
                <CardContent className="flex min-h-[500px] items-center justify-center p-8">
                  <div className="text-center space-y-4">
                    <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-muted">
                      <GitBranch className="h-8 w-8 text-muted-foreground" />
                    </div>
                    <div>
                      <p className="text-lg font-medium">Visual Workflow Builder</p>
                      <p className="text-sm text-muted-foreground mt-1">
                        Drag and drop nodes to create your workflow.
                        <br />
                        Connect nodes to define the flow of your calls.
                      </p>
                    </div>
                    <div className="flex items-center justify-center gap-3 pt-4">
                      <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                        <span className="h-3 w-3 rounded-full bg-green-500" />
                        Trigger
                      </div>
                      <div className="text-muted-foreground">&rarr;</div>
                      <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                        <span className="h-3 w-3 rounded-full bg-blue-500" />
                        Action
                      </div>
                      <div className="text-muted-foreground">&rarr;</div>
                      <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                        <span className="h-3 w-3 rounded-full bg-orange-500" />
                        Condition
                      </div>
                      <div className="text-muted-foreground">&rarr;</div>
                      <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                        <span className="h-3 w-3 rounded-full bg-red-500" />
                        End
                      </div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="json">
              <Card>
                <CardContent className="p-4">
                  <Textarea
                    value={workflowJson}
                    onChange={(e) => setWorkflowJson(e.target.value)}
                    className="min-h-[500px] font-mono text-sm"
                  />
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  );
}
