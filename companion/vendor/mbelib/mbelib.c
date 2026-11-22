/*
 * Copyright (C) 2010 mbelib Author
 * GPG Key ID: 0xEA5EFE2C (9E7A 5527 9CDC EBF7 BF1B  D772 4F98 E863 EA5E FE2C)
 *
 * Permission to use, copy, modify, and/or distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND ISC DISCLAIMS ALL WARRANTIES WITH
 * REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
 * AND FITNESS.  IN NO EVENT SHALL ISC BE LIABLE FOR ANY SPECIAL, DIRECT,
 * INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
 * LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
 * OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
 * PERFORMANCE OF THIS SOFTWARE.
 */

#include <stdlib.h>
#include <stdio.h>
#include <math.h>

#include "mbelib.h"
#include "mbelib_const.h"

/**
 * \return A pseudo-random float between [0.0, 1.0].
 * See http://www.azillionmonkeys.com/qed/random.html for further improvements
 */
static float
mbe_rand()
{
  return ((float) rand () / (float) RAND_MAX);
}

/**
 * \return A pseudo-random float between [-pi, +pi].
 */
static float
mbe_rand_phase()
{
  return mbe_rand() * (((float)M_PI) * 2.0F) - ((float)M_PI);
}

void
mbe_printVersion (char *str)
{
  sprintf (str, "%s", MBELIB_VERSION);
}

//renamed prev_mp and cur_mp parameters for move and last to make it clearer which parameter is being affected
//I had errouneously thought there was an error when init called move, but the input value of prev and cur were opposite
//I believe this has fixed that error and also helps alleviate confusion for what move and last do
void mbe_moveMbeParms (mbe_parms * in, mbe_parms * out)
{

  int l;
  out->swn = in->swn;
  out->w0 = in->w0;
  out->L = in->L;
  out->K = in->K;
  out->Ml[0] = (float) 0;
  out->gamma = in->gamma;
  out->repeat = in->repeat;
  for (l = 0; l <= 56; l++)
    {
      out->Ml[l] = in->Ml[l];
      out->Vl[l] = in->Vl[l];
      out->log2Ml[l] = in->log2Ml[l];
      out->PHIl[l] = in->PHIl[l];
      out->PSIl[l] = in->PSIl[l];
    }
}

void mbe_useLastMbeParms (mbe_parms * out, mbe_parms * in)
{

  int l;
  out->swn = in->swn;
  out->w0 = in->w0;
  out->L = in->L;
  out->K = in->K;
  out->Ml[0] = (float) 0;
  out->gamma = in->gamma;
  out->repeat = in->repeat;
  for (l = 0; l <= 56; l++)
    {
      out->Ml[l] = in->Ml[l];
      out->Vl[l] = in->Vl[l];
      out->log2Ml[l] = in->log2Ml[l];
      out->PHIl[l] = in->PHIl[l];
      out->PSIl[l] = in->PSIl[l];
    }
}

void mbe_initMbeParms (mbe_parms * cur_mp, mbe_parms * prev_mp, mbe_parms * prev_mp_enhanced)
{

  int l;
  prev_mp->swn = 0;
  prev_mp->w0 = 0.09378;
  prev_mp->L = 30;
  prev_mp->K = 10;
  prev_mp->gamma = (float) 0;
  for (l = 0; l <= 56; l++)
    {
      prev_mp->Ml[l] = (float) 0;
      prev_mp->Vl[l] = 0;
      prev_mp->log2Ml[l] = (float) 0;   // log2 of 1 == 0
      prev_mp->PHIl[l] = (float) 0;
      prev_mp->PSIl[l] = (M_PI / (float) 2);
    }
  prev_mp->repeat = 0;
  mbe_moveMbeParms (prev_mp, cur_mp); //when calling move, the first parameter is the input and the second is the value that is assigned the value of the input
  mbe_moveMbeParms (prev_mp, prev_mp_enhanced);
}

void
mbe_spectralAmpEnhance (mbe_parms * cur_mp)
{

  float Rm0, Rm1, R2m0, R2m1, Wl[57];
  int l;
  float sum, gamma, M;

  Rm0 = 0;
  Rm1 = 0;
  for (l = 1; l <= cur_mp->L; l++)
    {
      Rm0 = Rm0 + (cur_mp->Ml[l] * cur_mp->Ml[l]);
      Rm1 = Rm1 + ((cur_mp->Ml[l] * cur_mp->Ml[l]) * cosf (cur_mp->w0 * (float) l));
    }

  R2m0 = (Rm0*Rm0);
  R2m1 = (Rm1*Rm1);

  for (l = 1; l <= cur_mp->L; l++)
    {
      if (cur_mp->Ml[l] != 0)
        {
          Wl[l] = sqrtf (cur_mp->Ml[l]) * powf ((((float) 0.96 * M_PI * ((R2m0 + R2m1) - ((float) 2 * Rm0 * Rm1 * cosf (cur_mp->w0 * (float) l)))) / (cur_mp->w0 * Rm0 * (R2m0 - R2m1))), (float) 0.25);

          if ((8 * l) <= cur_mp->L)
            {
            }
          else if (Wl[l] > 1.2)
            {
              cur_mp->Ml[l] = 1.2 * cur_mp->Ml[l];
            }
          else if (Wl[l] < 0.5)
            {
              cur_mp->Ml[l] = 0.5 * cur_mp->Ml[l];
            }
          else
            {
              cur_mp->Ml[l] = Wl[l] * cur_mp->Ml[l];
            }
        }
    }

  // generate scaling factor
  sum = 0;
  for (l = 1; l <= cur_mp->L; l++)
    {
      M = cur_mp->Ml[l];
      if (M < 0)
        {
          M = -M;
        }
      sum += (M*M);
    }
  if (sum == 0)
    {
      gamma = (float) 1.0;
    }
  else
    {
      gamma = sqrtf (Rm0 / sum);
    }

  // apply scaling factor
  for (l = 1; l <= cur_mp->L; l++)
    {
      cur_mp->Ml[l] = gamma * cur_mp->Ml[l];
    }
}

//adapted from Boatbod OP25
void mbe_synthesizeTonef (float *aout_buf, char *ambe_d, mbe_parms * cur_mp)
{
  int i, n;
  float *aout_buf_p;

  int u0, u1, u2, u3;
  u0 = u1 = u2 = u3 = 0;

  for (i = 0; i < 12; i++)
  {
    u0 = u0 << 1;
    u0 = u0 | (int) ambe_d[i];
  }

  for (i = 12; i < 24; i++)
  {
    u1 = u1 << 1;
    u1 = u1 | (int) ambe_d[i];
  }

  for (i = 24; i < 35; i++)
  {
    u2 = u2 << 1;
    u2 = u2 | (int) ambe_d[i];
  }

  for (i = 35; i < 49; i++)
  {
    u3 = u3 << 1;
    u3 = u3 | (int) ambe_d[i];
  }

  int AD, ID0, ID1, ID2, ID3, ID4;
  AD = ((u0 & 0x3f) << 1) + ((u3 >> 4) & 0x1);
  ID0 = 0;
  ID1 = ((u1 & 0xfff) >> 4);
  ID2 = ((u1 & 0xf) << 4) + ((u2 >> 7) & 0xf);
  ID3 = ((u2 & 0x7f) << 1) + ((u2 >> 13) & 0x1);
  ID4 = ((u3 & 0x1fe0) >> 5);

  //TODO: Checkdown for matching ID values similar to Boatbod
  //For now, just going to rely on err values

  int en;
  float step1, step2, sample, amplitude;
  float freq1 = 0, freq2 = 0;

  #ifdef DISABLE_AMBE_TONES //generate silence if tones disabled
  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
  {
    *aout_buf_p = (float) 0;
    aout_buf_p++;
  }
  return;
  #endif

  switch(ID1) //using ID1 as a placeholder until check is coded
  {
    // single tones, set frequency
    case 5:
        freq1 = 156.25; freq2 = freq1;
        break;
    case 6:
        freq1 = 187.5; freq2 = freq1;
        break;
    // DTMF
    case 128:
        freq1 = 1336; freq2 = 941;
        break;
    case 129:
        freq1 = 1209; freq2 = 697;
        break;
    case 130:
        freq1 = 1336; freq2 = 697;
        break;
    case 131:
        freq1 = 1477; freq2 = 697;
        break;
    case 132:
        freq1 = 1209; freq2 = 770;
        break;
    case 133:
        freq1 = 1336; freq2 = 770;
        break;
    case 134:
        freq1 = 1477; freq2 = 770;
        break;
    case 135:
        freq1 = 1209; freq2 = 852;
        break;
    case 136:
        freq1 = 1336; freq2 = 852;
        break;
    case 137:
        freq1 = 1477; freq2 = 852;
        break;
    case 138:
        freq1 = 1633; freq2 = 697;
        break;
    case 139:
        freq1 = 1633; freq2 = 770;
        break;
    case 140:
        freq1 = 1633; freq2 = 852;
        break;
    case 141:
        freq1 = 1633; freq2 = 941;
        break;
    case 142:
        freq1 = 1209; freq2 = 941;
        break;
    case 143:
        freq1 = 1477; freq2 = 941;
        break;
    // KNOX
    case 144:
        freq1 = 1162; freq2 = 820;
        break;
    case 145:
        freq1 = 1052; freq2 = 606;
        break;
    case 146:
        freq1 = 1162; freq2 = 606;
        break;
    case 147:
        freq1 = 1279; freq2 = 606;
        break;
    case 148:
        freq1 = 1052; freq2 = 672;
        break;
    case 149:
        freq1 = 1162; freq2 = 672;
        break;
    case 150:
        freq1 = 1279; freq2 = 672;
        break;
    case 151:
        freq1 = 1052; freq2 = 743;
        break;
    case 152:
        freq1 = 1162; freq2 = 743;
        break;
    case 153:
        freq1 = 1279; freq2 = 743;
        break;
    case 154:
        freq1 = 1430; freq2 = 606;
        break;
    case 155:
        freq1 = 1430; freq2 = 672;
        break;
    case 156:
        freq1 = 1430; freq2 = 743;
        break;
    case 157:
        freq1 = 1430; freq2 = 820;
        break;
    case 158:
        freq1 = 1052; freq2 = 820;
        break;
    case 159:
        freq1 = 1279; freq2 = 820;
        break;
    // dual tones
    case 160:
        freq1 = 440; freq2 = 350;
        break;
    case 161:
        freq1 = 480; freq2 = 440;
        break;
    case 162:
        freq1 = 620; freq2 = 480;
        break;
    case 163:
        freq1 = 490; freq2 = 350;
        break;
    // zero amplitude
    case 255:
        freq1 = 0; freq2 = 0;
        break;
    // single tones, calculated frequency
    default:
        if ((ID1 >= 7) && (ID1 <= 122))
        {
          freq1 = 31.25 * ID1; freq2 = freq1;
        }
  }

  // Zero Amplitude and unimplemented tones
  if ((freq1 == 0) && (freq2 == 0))
  {
    aout_buf_p = aout_buf;
    for (n = 0; n < 160; n++)
    {
      *aout_buf_p = (float) 0;
      aout_buf_p++;
    }
    return;
  }

  //debug
  // fprintf (stderr, "TONE ID = %d AD = %d \n", ID1, AD);

  // Synthesize tones
  step1 = 2 * M_PI * freq1 / 8000.0f;
  step2 = 2 * M_PI * freq2 / 8000.0f;
  amplitude = AD * 75.0f; //
  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
  {
    *aout_buf_p = (float) ( amplitude * (sin((cur_mp->swn) * step1)/2 + sin((cur_mp->swn) * step2)/2) );
    *aout_buf_p = *aout_buf_p / 6.0f;
    aout_buf_p++;
    cur_mp->swn++;
  }
}

//simplified version for single frequency dstar based on previous code writers guessword
void mbe_synthesizeTonefdstar (float *aout_buf, char *ambe_d, mbe_parms * cur_mp, int ID1)
{
  int i, n;
  float *aout_buf_p;

  int AD = 103; //amplitude, just using a value I've seen in other tones
  float step1, step2, sample, amplitude;
  float freq1 = 0, freq2 = 0;

  #ifdef DISABLE_AMBE_TONES //generate silence if tones disabled
  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
  {
    *aout_buf_p = (float) 0;
    aout_buf_p++;
  }
  return;
  #endif

  switch(ID1)
  {
    // single tones, set frequency
    case 5:
        freq1 = 156.25; freq2 = freq1;
        break;
    case 6:
        freq1 = 187.5; freq2 = freq1;
        break;
    // single tones, calculated frequency
    default:
        if ((ID1 >= 7) && (ID1 <= 122))
        {
          freq1 = 31.25 * ID1; freq2 = freq1;
        }
  }

  //debug
  // fprintf (stderr, "TONE ID = %d AD = %d \n", ID1, AD);

  // Synthesize tones
  step1 = 2 * M_PI * freq1 / 8000.0f;
  step2 = 2 * M_PI * freq2 / 8000.0f;
  amplitude = AD * 75.0f; //
  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
  {
    *aout_buf_p = (float) ( amplitude * (sin((cur_mp->swn) * step1)/2 + sin((cur_mp->swn) * step2)/2) );
    *aout_buf_p = *aout_buf_p / 6.0f;
    aout_buf_p++;
    cur_mp->swn++;
  }
}

void
mbe_synthesizeSilencef (float *aout_buf)
{

  int n;
  float *aout_buf_p;

  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
    {
      *aout_buf_p = (float) 0;
      aout_buf_p++;
    }
}

void
mbe_synthesizeSilence (short *aout_buf)
{

  int n;
  short *aout_buf_p;

  aout_buf_p = aout_buf;
  for (n = 0; n < 160; n++)
    {
      *aout_buf_p = (short) 0;
      aout_buf_p++;
    }
}

void
mbe_synthesizeSpeechf (float *aout_buf, mbe_parms * cur_mp, mbe_parms * prev_mp, int uvquality)
{

  int i, l, n, maxl;
  float *Ss, loguvquality;
  float C1, C2, C3, C4;
  float deltaphil, deltawl, thetaln, aln;
  int numUv;
  float cw0, pw0, cw0l, pw0l;
  float uvsine, uvrand, uvthreshold, uvthresholdf;
  float uvstep, uvoffset;
  float qfactor;
  float rphase[64], rphase2[64];

  const int N = 160;

  uvthresholdf = (float) 2700;
  uvthreshold = ((uvthresholdf * M_PI) / (float) 4000);

  // voiced/unvoiced/gain settings
  uvsine = (float) 1.3591409 *M_E;
  uvrand = (float) 2.0;

  if ((uvquality < 1) || (uvquality > 64))
    {
      fprintf (stderr,"\nmbelib: Error - uvquality must be within the range 1 - 64, setting to default value of 3\n");
      uvquality = 3;
    }

  // calculate loguvquality
  if (uvquality == 1)
    {
      loguvquality = (float) 1 / M_E;
    }
  else
    {
      loguvquality = log ((float) uvquality) / (float) uvquality;
    }

  // calculate unvoiced step and offset values
  uvstep = (float) 1.0 / (float) uvquality;
  qfactor = loguvquality;
  uvoffset = (uvstep * (float) (uvquality - 1)) / (float) 2;

  // count number of unvoiced bands
  numUv = 0;
  for (l = 1; l <= cur_mp->L; l++)
    {
      if (cur_mp->Vl[l] == 0)
        {
          numUv++;
        }
    }

  cw0 = cur_mp->w0;
  pw0 = prev_mp->w0;

  // init aout_buf
  Ss = aout_buf;
  for (n = 0; n < N; n++)
    {
      *Ss = (float) 0;
      Ss++;
    }

  // eq 128 and 129
  if (cur_mp->L > prev_mp->L)
    {
      maxl = cur_mp->L;
      for (l = prev_mp->L + 1; l <= maxl; l++)
        {
          prev_mp->Ml[l] = (float) 0;
          prev_mp->Vl[l] = 1;
        }
    }
  else
    {
      maxl = prev_mp->L;
      for (l = cur_mp->L + 1; l <= maxl; l++)
        {
          cur_mp->Ml[l] = (float) 0;
          cur_mp->Vl[l] = 1;
        }
    }

  // update phil from eq 139,140
  for (l = 1; l <= 56; l++)
    {
      cur_mp->PSIl[l] = prev_mp->PSIl[l] + ((pw0 + cw0) * ((float) (l * N) / (float) 2));
      if (l <= (int) (cur_mp->L / 4))
        {
          cur_mp->PHIl[l] = cur_mp->PSIl[l];
        }
      else
        {
          cur_mp->PHIl[l] = cur_mp->PSIl[l] + ((numUv * mbe_rand_phase()) / cur_mp->L);
        }
    }

  for (l = 1; l <= maxl; l++)
    {
      cw0l = (cw0 * (float) l);
      pw0l = (pw0 * (float) l);
      if ((cur_mp->Vl[l] == 0) && (prev_mp->Vl[l] == 1))
        {
          Ss = aout_buf;
          // init random phase
          for (i = 0; i < uvquality; i++)
            {
              rphase[i] = mbe_rand_phase();
            }
          for (n = 0; n < N; n++)
            {
              C1 = 0;
              // eq 131
              C1 = Ws[n + N] * prev_mp->Ml[l] * cosf ((pw0l * (float) n) + prev_mp->PHIl[l]);
              C3 = 0;
              // unvoiced multisine mix
              for (i = 0; i < uvquality; i++)
                {
                  C3 = C3 + cosf ((cw0 * (float) n * ((float) l + ((float) i * uvstep) - uvoffset)) + rphase[i]);
                  if (cw0l > uvthreshold)
                    {
                      C3 = C3 + ((cw0l - uvthreshold) * uvrand * mbe_rand());
                    }
                }
              C3 = C3 * uvsine * Ws[n] * cur_mp->Ml[l] * qfactor;
              *Ss = *Ss + C1 + C3;
              Ss++;
            }
        }
      else if ((cur_mp->Vl[l] == 1) && (prev_mp->Vl[l] == 0))
        {
          Ss = aout_buf;
          // init random phase
          for (i = 0; i < uvquality; i++)
            {
              rphase[i] = mbe_rand_phase();
            }
          for (n = 0; n < N; n++)
            {
              C1 = 0;
              // eq 132
              C1 = Ws[n] * cur_mp->Ml[l] * cosf ((cw0l * (float) (n - N)) + cur_mp->PHIl[l]);
              C3 = 0;
              // unvoiced multisine mix
              for (i = 0; i < uvquality; i++)
                {
                  C3 = C3 + cosf ((pw0 * (float) n * ((float) l + ((float) i * uvstep) - uvoffset)) + rphase[i]);
                  if (pw0l > uvthreshold)
                    {
                      C3 = C3 + ((pw0l - uvthreshold) * uvrand * mbe_rand());
                    }
                }
              C3 = C3 * uvsine * Ws[n + N] * prev_mp->Ml[l] * qfactor;
              *Ss = *Ss + C1 + C3;
              Ss++;
            }
        }
//      else if (((cur_mp->Vl[l] == 1) || (prev_mp->Vl[l] == 1)) && ((l >= 8) || (fabsf (cw0 - pw0) >= ((float) 0.1 * cw0))))
      else if ((cur_mp->Vl[l] == 1) || (prev_mp->Vl[l] == 1))
        {
          Ss = aout_buf;
          for (n = 0; n < N; n++)
            {
              C1 = 0;
              // eq 133-1
              C1 = Ws[n + N] * prev_mp->Ml[l] * cosf ((pw0l * (float) n) + prev_mp->PHIl[l]);
              C2 = 0;
              // eq 133-2
              C2 = Ws[n] * cur_mp->Ml[l] * cosf ((cw0l * (float) (n - N)) + cur_mp->PHIl[l]);
              *Ss = *Ss + C1 + C2;
              Ss++;
            }
        }
/*
      // expensive and unnecessary?
      else if ((cur_mp->Vl[l] == 1) || (prev_mp->Vl[l] == 1))
        {
          Ss = aout_buf;
          // eq 137
          deltaphil = cur_mp->PHIl[l] - prev_mp->PHIl[l] - (((pw0 + cw0) * (float) (l * N)) / (float) 2);
          // eq 138
          deltawl = ((float) 1 / (float) N) * (deltaphil - ((float) 2 * M_PI * (int) ((deltaphil + M_PI) / (M_PI * (float) 2))));
          for (n = 0; n < N; n++)
            {
              // eq 136
              thetaln = prev_mp->PHIl[l] + ((pw0l + deltawl) * (float) n) + (((cw0 - pw0) * ((float) (l * n * n)) / (float) (2 * N)));
              // eq 135
              aln = prev_mp->Ml[l] + (((float) n / (float) N) * (cur_mp->Ml[l] - prev_mp->Ml[l]));
              // eq 134
              *Ss = *Ss + (aln * cosf (thetaln));
              Ss++;
            }
        }
*/
      else
        {
          Ss = aout_buf;
          // init random phase
          for (i = 0; i < uvquality; i++)
            {
              rphase[i] = mbe_rand_phase();
            }
          // init random phase
          for (i = 0; i < uvquality; i++)
            {
              rphase2[i] = mbe_rand_phase();
            }
          for (n = 0; n < N; n++)
            {
              C3 = 0;
              // unvoiced multisine mix
              for (i = 0; i < uvquality; i++)
                {
                  C3 = C3 + cosf ((pw0 * (float) n * ((float) l + ((float) i * uvstep) - uvoffset)) + rphase[i]);
                  if (pw0l > uvthreshold)
                    {
                      C3 = C3 + ((pw0l - uvthreshold) * uvrand * mbe_rand());
                    }
                }
              C3 = C3 * uvsine * Ws[n + N] * prev_mp->Ml[l] * qfactor;
              C4 = 0;
              // unvoiced multisine mix
              for (i = 0; i < uvquality; i++)
                {
                  C4 = C4 + cosf ((cw0 * (float) n * ((float) l + ((float) i * uvstep) - uvoffset)) + rphase2[i]);
                  if (cw0l > uvthreshold)
                    {
                      C4 = C4 + ((cw0l - uvthreshold) * uvrand * mbe_rand());
                    }
                }
              C4 = C4 * uvsine * Ws[n] * cur_mp->Ml[l] * qfactor;
              *Ss = *Ss + C3 + C4;
              Ss++;
            }
        }
    }
}

void
mbe_synthesizeSpeech (short *aout_buf, mbe_parms * cur_mp, mbe_parms * prev_mp, int uvquality)
{
  float float_buf[160];

  mbe_synthesizeSpeechf (float_buf, cur_mp, prev_mp, uvquality);
  mbe_floattoshort (float_buf, aout_buf);
}

void
mbe_floattoshort (float *float_buf, short *aout_buf)
{

  short *aout_buf_p;
  float *float_buf_p;
  int i, again;
  float audio;

  again = 7;
  aout_buf_p = aout_buf;
  float_buf_p = float_buf;
  for (i = 0; i < 160; i++)
    {
      audio = again * *float_buf_p;
      if (audio > 32760)
        {
#ifdef MBE_DEBUG
          fprintf (stderr,"audio clip: %f\n", audio);
#endif
          audio = 32760;
        }
      else if (audio < -32760)
        {
#ifdef MBE_DEBUG
          fprintf (stderr,"audio clip: %f\n", audio);
#endif
          audio = -32760;
        }
      *aout_buf_p = (short) (audio);
      aout_buf_p++;
      float_buf_p++;
    }
}
