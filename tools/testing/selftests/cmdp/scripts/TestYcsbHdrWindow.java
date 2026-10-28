import java.io.*;
import org.HdrHistogram.*;
public class TestYcsbHdrWindow {
  public static void main(String[] args) throws Exception {
    long base = 1700000000000L;
    File dir = new File(args[0]);
    dir.mkdirs();
    try (PrintStream stream = new PrintStream(new File(dir, "READ.hdr"))) {
      HistogramLogWriter writer = new HistogramLogWriter(stream);
      writer.outputLogFormatVersion();
      writer.outputStartTime(base);
      writer.setBaseTime(base);
      writer.outputLegend();
      for (int i=0; i<5; i++) {
        Histogram h = new Histogram(3);
        h.recordValueWithCount(i < 2 ? 9999 : 10, 10);
        h.setStartTimeStamp(base + i * 1000);
        h.setEndTimeStamp(base + (i + 1) * 1000);
        writer.outputIntervalHistogram(h);
      }
    }
    YcsbHdrWindow.main(new String[]{args[0], Long.toString(base+2000), Long.toString(base+5000)});
  }
}
