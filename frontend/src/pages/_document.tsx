import Document, {
  type DocumentContext,
  type DocumentInitialProps,
  Head,
  Html,
  Main,
  NextScript,
} from 'next/document';

type NoncedDocumentProps = DocumentInitialProps & { nonce?: string };

export default class NoncedDocument extends Document<NoncedDocumentProps> {
  static async getInitialProps(context: DocumentContext): Promise<NoncedDocumentProps> {
    const initialProps = await Document.getInitialProps(context);
    const nonceHeader = context.req?.headers['x-nonce'];
    const nonce = Array.isArray(nonceHeader) ? nonceHeader[0] : nonceHeader;
    return { ...initialProps, nonce };
  }

  render() {
    const { nonce } = this.props;
    return (
      <Html lang="en" dir="ltr">
        <Head nonce={nonce}>
          <meta name="theme-color" content="#111525" />
          <meta name="color-scheme" content="light" />
        </Head>
        <body>
          <Main />
          <NextScript nonce={nonce} />
        </body>
      </Html>
    );
  }
}
