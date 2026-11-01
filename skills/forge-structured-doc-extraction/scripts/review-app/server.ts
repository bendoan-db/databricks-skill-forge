// Server of the forge-structured-doc-extraction review app (module 07): the documents volume, read-only, and Lakebase.
import { createApp, files, lakebase, server } from '@databricks/appkit';
import { registerReviewRoutes, setupReviewTables } from './review-routes';

createApp({
  plugins: [
    files({ volumes: { files: { policy: files.policy.publicRead() } } }), // reviewers view documents; nothing writes
    lakebase(),
    server(),
  ],
  async onPluginsReady(appkit) {
    await setupReviewTables(appkit);
    registerReviewRoutes(appkit);
  },
}).catch(console.error);
